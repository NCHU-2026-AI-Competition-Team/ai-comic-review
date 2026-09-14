"""AI 漫审 VLM 主审服务：Modal GPU 云端部署 Qwen3-VL-8B-Instruct。

部署形态借鉴参考项目 modal-qwen（app 定义 + CUDA 基础镜像 + uv 装包 +
HF 权重 Volume 缓存 + HTTPS web endpoint），业务逻辑为本仓审核契约。

- 模型：Qwen/Qwen3-VL-8B-Instruct（Apache-2.0，非 gated），BF16 精度基线，
  禁止为省显存直接上 INT8/INT4（审核 Recall 优先）。
- GPU：L40S（48GB）。A10G（22GB）实测 OOM，精度纪律禁止 INT8/INT4。
- 接口：POST /review（multipart 多帧 + 文本表单字段），输出严格 JSON，
  由 vLLM 结构化输出（JSON Schema guided decoding）保证格式。
- 高分辨率帧：解码后按长边 768 降采样再进 vLLM，避免图像 token 撞 max_model_len。
- escalation 预留：model 表单字段收到 32B 标识时返回明确 501。

部署：cd modal/vlm && modal deploy serve.py
"""

import modal

from review_contract import (
    ESCALATION_MODEL_ID,
    FRAME_LONG_EDGE,
    MAX_FRAME_BYTES,
    MAX_FRAMES_PER_REQUEST,
    MAX_MODEL_LEN,
    MAX_OUTPUT_TOKENS,
    PRIMARY_HF_REPO,
    PRIMARY_MODEL_ID,
    REVIEW_JSON_SCHEMA,
    InputValidationError,
    ResultValidationError,
    UnknownModelError,
    build_review_prompt,
    ensure_review_fits_model_len,
    fit_long_edge,
    is_model_len_error,
    parse_frame_timestamps,
    resolve_model_role,
    validate_review_result,
    validate_text_fields,
    MODEL_ROLE_ESCALATION,
)

MINUTES = 60

# --- 服务参数（端点/模型名等契约常量在 review_contract.py 唯一定义） ----------
APP_NAME = "ai-comic-review-vlm"
# A10G（22GB）实测 OOM：BF16 权重 17.7GB + vLLM 多帧 ViT profiling 峰值 4.6GB 放不下；
# 精度纪律禁止 INT8/INT4 省显存，故按实际需求升级到 L40S（48GB）
GPU_TYPE = "L40S"               # 48GB；8B BF16 权重约 16GB + ViT 峰值 + KV cache
# MAX_MODEL_LEN / MAX_OUTPUT_TOKENS 在 review_contract.py 唯一定义
SCALEDOWN_WINDOW_SECONDS = 300  # 空闲 5 分钟自动缩容，控制成本
MAX_CONCURRENT_INPUTS = 4

# --- 镜像：CUDA 基础镜像 + vLLM 稳定版（v0.11.0 起原生支持 Qwen3-VL） ---------
vlm_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.0-devel-ubuntu22.04",
        add_python="3.12",
    )
    .entrypoint([])
    .uv_pip_install(
        "vllm==0.11.0",
        # Qwen3-VL 需 transformers>=4.57；v5 删除了 all_special_tokens_extended，
        # 与 vLLM 0.11.0 不兼容，必须卡上限 <5
        "transformers>=4.57.0,<5",
        "hf_transfer",           # 权重高速下载
        "fastapi[standard]",
        "python-multipart",      # multipart 表单解析
        "pillow",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
    .add_local_python_source("review_contract")
)

# --- 权重缓存 Volume：首次冷启动下载后持久化，后续启动直接命中 -----------------
hf_cache_vol = modal.Volume.from_name("ai-comic-review-vlm-hf-cache", create_if_missing=True)

app = modal.App(name=APP_NAME)


@app.cls(
    image=vlm_image,
    gpu=GPU_TYPE,
    volumes={"/root/.cache/huggingface": hf_cache_vol},
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    timeout=20 * MINUTES,
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
class VlmReviewer:
    """Qwen3-VL-8B 主审：容器启动时加载权重（enter），web 请求复用同一引擎。"""

    @modal.enter()
    def load(self) -> None:
        import threading

        from transformers import AutoProcessor
        from vllm import LLM

        # BF16 精度基线；enforce_eager 跳过 CUDA graph 采集，省显存、加快冷启动
        self._llm = LLM(
            model=PRIMARY_HF_REPO,
            dtype="bfloat16",
            max_model_len=MAX_MODEL_LEN,
            enforce_eager=True,
            gpu_memory_utilization=0.90,
            limit_mm_per_prompt={"image": MAX_FRAMES_PER_REQUEST},
        )
        self._processor = AutoProcessor.from_pretrained(PRIMARY_HF_REPO)
        # vLLM 离线引擎的 generate 非线程安全，并发请求串行进入
        self._lock = threading.Lock()

    def _review_frames(
        self,
        images: list,
        frame_timestamps_ms: list[int],
        ocr_text: str,
        asr_text: str,
        context: str,
        rules: str,
    ) -> dict:
        """对一组帧执行一次主审，返回校验归一化后的契约 JSON。"""
        import json

        from vllm import SamplingParams
        from vllm.sampling_params import StructuredOutputsParams

        prompt_text = build_review_prompt(
            frame_timestamps_ms, ocr_text=ocr_text, asr_text=asr_text,
            context=context, rules=rules,
        )
        # 降采样后仍可能因超长 OCR/ASR 顶满上下文：先给可读 4xx，避免 vLLM 裸 500
        ensure_review_fits_model_len(
            [(image.width, image.height) for image in images],
            prompt_text,
            max_model_len=MAX_MODEL_LEN,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        messages = [
            {
                "role": "user",
                "content": [
                    *[{"type": "image"} for _ in images],
                    {"type": "text", "text": prompt_text},
                ],
            }
        ]
        templated = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        sampling_params = SamplingParams(
            temperature=0.0,  # 审核场景取贪心，保证确定性
            max_tokens=MAX_OUTPUT_TOKENS,
            structured_outputs=StructuredOutputsParams(json=REVIEW_JSON_SCHEMA),
        )
        request = {"prompt": templated, "multi_modal_data": {"image": images}}

        # 结构化输出已基本保证格式；仍兜底重试一次，失败抛 ResultValidationError
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                with self._lock:
                    outputs = self._llm.generate([request], sampling_params)
            except Exception as exc:
                if is_model_len_error(exc):
                    raise InputValidationError(
                        "图像 token 估算超限（引擎拒绝）："
                        f"max_model_len={MAX_MODEL_LEN}，细节：{exc}"
                    ) from exc
                raise
            raw_text = outputs[0].outputs[0].text
            try:
                return validate_review_result(json.loads(raw_text))
            except (json.JSONDecodeError, ResultValidationError) as exc:
                last_error = exc
        raise ResultValidationError(f"模型输出两次均不合规：{last_error}")

    @modal.asgi_app()
    def web(self):
        from fastapi import FastAPI, File, Form, UploadFile
        from fastapi.responses import JSONResponse

        api = FastAPI(title="AI 漫审 VLM 主审", version="0.1.0")

        @api.get("/health")
        def health() -> dict:
            return {
                "status": "ok",
                "model": PRIMARY_MODEL_ID,
                "hf_repo": PRIMARY_HF_REPO,
                "gpu": GPU_TYPE,
                "escalation_model": ESCALATION_MODEL_ID,
                "escalation_deployed": False,
            }

        @api.post("/review")
        async def review(
            frames: list[UploadFile] = File(...),
            model: str = Form(""),
            ocr_text: str = Form(""),
            asr_text: str = Form(""),
            context: str = Form(""),
            rules: str = Form(""),
            frame_timestamps_ms: str = Form(""),
        ):
            import io

            from PIL import Image

            # --- 模型路由：32B 为 escalation 预留，本期明确 501 ---
            try:
                role = resolve_model_role(model)
            except UnknownModelError as exc:
                return JSONResponse(status_code=400, content={"detail": str(exc)})
            if role == MODEL_ROLE_ESCALATION:
                return JSONResponse(
                    status_code=501,
                    content={
                        "detail": (
                            f"escalation 复审模型 {ESCALATION_MODEL_ID} 本期未部署，"
                            f"请改用 {PRIMARY_MODEL_ID}"
                        )
                    },
                )

            # --- 输入校验：畸形输入明确 4xx ---
            if not frames:
                return JSONResponse(status_code=400, content={"detail": "frames 字段至少上传 1 帧图片"})
            if len(frames) > MAX_FRAMES_PER_REQUEST:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"frames 最多 {MAX_FRAMES_PER_REQUEST} 帧，实际 {len(frames)} 帧"},
                )
            try:
                validate_text_fields(
                    {"ocr_text": ocr_text, "asr_text": asr_text, "context": context, "rules": rules}
                )
                timestamps = parse_frame_timestamps(frame_timestamps_ms, len(frames))
            except InputValidationError as exc:
                return JSONResponse(status_code=400, content={"detail": str(exc)})

            images = []
            for index, frame in enumerate(frames):
                payload = await frame.read()
                if not payload:
                    return JSONResponse(status_code=400, content={"detail": f"frames[{index}] 内容为空"})
                if len(payload) > MAX_FRAME_BYTES:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": f"frames[{index}] 超过单帧大小上限 {MAX_FRAME_BYTES} 字节"},
                    )
                try:
                    image = Image.open(io.BytesIO(payload))
                    image.load()
                    image = image.convert("RGB")
                except Exception:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": f"frames[{index}] 不是可解码的图片文件"},
                    )
                try:
                    target = fit_long_edge(image.width, image.height, FRAME_LONG_EDGE)
                except InputValidationError as exc:
                    return JSONResponse(status_code=400, content={"detail": str(exc)})
                if (image.width, image.height) != target:
                    image = image.resize(target, Image.Resampling.LANCZOS)
                images.append(image)

            # --- 推理：输入超限明确 4xx；输出不合规时明确 502 ---
            try:
                result = self._review_frames(
                    images, timestamps, ocr_text, asr_text, context, rules,
                )
            except InputValidationError as exc:
                return JSONResponse(status_code=400, content={"detail": str(exc)})
            except ResultValidationError as exc:
                return JSONResponse(
                    status_code=502,
                    content={"detail": f"模型输出不合规，已重试仍失败：{exc}"},
                )
            return JSONResponse(content=result)

        return api


@app.local_entrypoint()
def main() -> None:
    """modal run serve.py：打印服务端点，便于部署后手工验证。"""
    reviewer = VlmReviewer()
    print(f"endpoint: {reviewer.web.get_web_url()}")
    print("POST {endpoint}/review  multipart: frames(多文件) + model/ocr_text/asr_text/context/rules")
