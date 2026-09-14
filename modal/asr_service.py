"""Modal 云端 Qwen3-ASR 服务：转写 + ForcedAligner 时间戳，暴露 POST /transcribe。

部署形态对齐参考项目 modal-qwen（App / GPU / Image / Volume / Secret / HTTPS），
业务契约对齐本仓 ai/asr/qwen_asr.py 与 ai/asr/base.py。

模型（HuggingFace 核实，禁止替换）：
  - Qwen/Qwen3-ASR-1.7B（Apache-2.0）
  - Qwen/Qwen3-ForcedAligner-0.6B（Apache-2.0）
推理走官方 qwen-asr：Qwen3ASRModel.from_pretrained(..., forced_aligner=...).
"""

import importlib.util
import logging
import os
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Union

import modal

logger = logging.getLogger(__name__)

APP_NAME = "ai-comic-review-asr"
ASR_REPO_ID = "Qwen/Qwen3-ASR-1.7B"
ALIGNER_REPO_ID = "Qwen/Qwen3-ForcedAligner-0.6B"
# 1.7B + 0.6B 级；A10G 原生 bf16，T4 回退 fp16。列表表示配额不足时按序回退。
GPU_TYPE: Union[str, list] = ["A10G", "T4"]
HF_CACHE_DIR = "/root/.cache/huggingface"
LOGIC_REMOTE_PATH = "/root/asr_logic.py"
MINUTES = 60

_LOGIC_LOCAL = Path(__file__).resolve().parent / "asr_logic.py"

hf_cache_vol = modal.Volume.from_name("huggingface-cache", create_if_missing=True)

asr_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "sox", "libsndfile1", "libsox-fmt-all")
    .uv_pip_install(
        "torch",
        "torchaudio",
        index_url="https://download.pytorch.org/whl/cu124",
    )
    .uv_pip_install(
        "qwen-asr>=0.0.6",
        "transformers>=4.57.3",
        "accelerate",
        "qwen-omni-utils>=0.0.4",
        "librosa",
        "soundfile",
        "sox",
        "numpy",
        "fastapi",
        "python-multipart",
    )
    .env(
        {
            "HF_HOME": HF_CACHE_DIR,
            "HF_HUB_CACHE": f"{HF_CACHE_DIR}/hub",
            "TRANSFORMERS_CACHE": f"{HF_CACHE_DIR}/hub",
            "HF_XET_HIGH_PERFORMANCE": "1",
        }
    )
    .add_local_file(str(_LOGIC_LOCAL), LOGIC_REMOTE_PATH, copy=True)
)

app = modal.App(name=APP_NAME)


def _load_asr_logic() -> ModuleType:
    """按文件路径加载契约逻辑，避免把 modal/ 当成 Python 包。"""
    import sys

    candidates = (Path(LOGIC_REMOTE_PATH), _LOGIC_LOCAL)
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("asr_logic", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            # dataclass 解析注解时要能从 sys.modules 找到该模块
            sys.modules["asr_logic"] = module
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError("找不到 asr_logic.py")


def _select_dtype(torch_module):  # type: ignore[no-untyped-def]
    """Ampere+（含 A10G）用 bf16，T4 等旧卡用 fp16。"""
    if not torch_module.cuda.is_available():
        raise RuntimeError("容器未分配到 GPU，无法加载 Qwen3-ASR")
    major, _minor = torch_module.cuda.get_device_capability()
    if major >= 8:
        return torch_module.bfloat16
    return torch_module.float16


def _load_models():  # type: ignore[no-untyped-def]
    """加载官方 ASR + ForcedAligner，权重写入 Volume 缓存。"""
    import torch
    from qwen_asr import Qwen3ASRModel

    dtype = _select_dtype(torch)
    device = "cuda:0"
    logger.info(
        "加载 ASR 模型 repo=%s aligner=%s dtype=%s gpu=%s",
        ASR_REPO_ID,
        ALIGNER_REPO_ID,
        dtype,
        torch.cuda.get_device_name(0),
    )
    model = Qwen3ASRModel.from_pretrained(
        ASR_REPO_ID,
        dtype=dtype,
        device_map=device,
        max_inference_batch_size=8,
        max_new_tokens=512,
        forced_aligner=ALIGNER_REPO_ID,
        forced_aligner_kwargs={
            "dtype": dtype,
            "device_map": device,
        },
    )
    return model


def _run_transcribe(model, wav_path: str):  # type: ignore[no-untyped-def]
    return model.transcribe(audio=wav_path, return_time_stamps=True)


@app.function(
    image=asr_image,
    gpu=GPU_TYPE,
    timeout=20 * MINUTES,
    startup_timeout=30 * MINUTES,
    scaledown_window=5 * MINUTES,
    max_containers=1,
    volumes={HF_CACHE_DIR: hf_cache_vol},
    secrets=[modal.Secret.from_name("huggingface-secret")],
)
@modal.asgi_app(label="ai-comic-review-asr")
def web():
    """HTTPS ASGI 入口：容器启动时加载模型，随后提供 /transcribe。"""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    logic = _load_asr_logic()
    asr_model = _load_models()
    try:
        hf_cache_vol.commit()
    except Exception:
        logger.warning("HuggingFace Volume commit 失败，下次冷启动可能重新下载", exc_info=True)

    api = FastAPI(title="ai-comic-review ASR", docs_url=None, redoc_url=None)

    @api.get("/health")
    def health():
        return {
            "status": "ok",
            "asr_repo": ASR_REPO_ID,
            "aligner_repo": ALIGNER_REPO_ID,
        }

    @api.post("/transcribe")
    async def transcribe(request: Request):
        # 手动解析 multipart，避开 pydantic ForwardRef(UploadFile) 在 FastAPI 0.141 的校验崩溃
        content_type = (request.headers.get("content-type") or "").lower()
        if "multipart/form-data" not in content_type:
            return JSONResponse(
                status_code=400,
                content={"error": "请求必须是 multipart/form-data，字段 file 与 model"},
            )
        form = await request.form()
        model_id = form.get("model")
        upload = form.get("file")
        try:
            logic.validate_model_id(model_id)
        except logic.AsrRequestError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                status_code=400,
                content={"error": "缺少 multipart 字段 file（16kHz 单声道 PCM wav）"},
            )
        try:
            data = await upload.read()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "读取上传文件失败"})

        try:
            wav_info = logic.validate_wav_bytes(data)
        except logic.AsrRequestError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                raw = _run_transcribe(asr_model, tmp_path)
            except Exception as exc:
                if logic.is_no_speech_error(exc):
                    return JSONResponse(
                        content={"segments": [], "language": logic.DEFAULT_LANGUAGE}
                    )
                logger.exception("Qwen3-ASR 推理失败")
                return JSONResponse(
                    status_code=500,
                    content={"error": "ASR 推理失败：%s" % type(exc).__name__},
                )
            payload = logic.normalize_asr_output(raw, wav_info.duration_ms)
            return JSONResponse(content=payload)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return api
