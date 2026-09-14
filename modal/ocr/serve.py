"""Modal OCR 服务：PP-OCRv6 主识别 + PaddleOCR-VL-1.6 低置信兜底。

HTTPS 契约与 ai/ocr/base.py 的 OcrTextLine 对齐：
POST /recognize（multipart 字段 file 上传 JPEG，表单字段 model 携带模型标识）
→ JSON {"lines":[{"text","bbox","confidence"}]}。
空图/无文字返回空 lines；畸形输入返回 4xx。
"""

import io
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import modal
import yaml
from fastapi import FastAPI, HTTPException, Request
from PIL import Image, UnidentifiedImageError

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from contract import needs_fallback, parse_ppocr_results, parse_vl_results

_config_path = _HERE / "config.yaml"
with open(_config_path, encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)

GPU_TYPE = CFG["gpu"]["type"]
GPU_COUNT = CFG["gpu"].get("count", 1)
APP_NAME = CFG["serving"].get("app_name", "ai-comic-review-ocr")
IDLE_TIMEOUT = CFG["serving"].get("container_idle_timeout", 300)
MAX_CONCURRENT = CFG["serving"].get("max_concurrent_requests", 1)
REQUEST_TIMEOUT = CFG["serving"].get("request_timeout", 300)
STARTUP_TIMEOUT = CFG["serving"].get("startup_timeout", 1800)
PRIMARY_MODEL = CFG["models"]["primary"]
FALLBACK_MODEL = CFG["models"]["fallback"]
OCR_LANG = CFG["models"].get("lang", "ch")
FALLBACK_THRESHOLD = float(CFG["models"].get("fallback_confidence_threshold", 0.6))
PADDLE_INDEX = CFG["image"]["paddle_index"]
PADDLE_PACKAGE = CFG["image"]["paddle_package"]
PYTHON_VERSION = CFG["image"].get("python", "3.11")

# PaddleX 3.7 实际写入 ~/.paddlex，Volume 必须挂在该路径才能缓存权重
PADDLEX_HOME = "/root/.paddlex"
HF_HOME = "/root/.paddlex/huggingface"
JPEG_MAGIC = b"\xff\xd8\xff"
ALLOWED_MODELS = frozenset({PRIMARY_MODEL, FALLBACK_MODEL})
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

logger = logging.getLogger(__name__)

ocr_image = (
    modal.Image.debian_slim(python_version=PYTHON_VERSION)
    .apt_install(
        "libgomp1",
        "libglib2.0-0",
        "libgl1",
        "libsm6",
        "libxext6",
        "libxrender1",
    )
    .uv_pip_install(
        PADDLE_PACKAGE,
        extra_options=f"--extra-index-url {PADDLE_INDEX}",
    )
    .uv_pip_install(
        "opencv-python-headless",
        "paddleocr[doc-parser]>=3.3.0",
        "fastapi",
        "python-multipart",
        "pillow",
        "numpy",
        "huggingface_hub",
        "pyyaml",
    )
    .env(
        {
            "PADDLEX_HOME": PADDLEX_HOME,
            "HF_HOME": HF_HOME,
            "HUGGINGFACE_HUB_CACHE": HF_HOME,
            "HF_HUB_CACHE": HF_HOME,
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "FLAGS_use_mkldnn": "0",
        }
    )
    .add_local_file(str(_config_path), remote_path="/root/config.yaml")
    .add_local_python_source("contract")
)

model_cache = modal.Volume.from_name("ai-comic-ocr-cache", create_if_missing=True)
app = modal.App(name=APP_NAME)
GPU_STR = f"{GPU_TYPE}:{GPU_COUNT}"


@app.cls(
    image=ocr_image,
    gpu=GPU_STR,
    timeout=REQUEST_TIMEOUT,
    scaledown_window=IDLE_TIMEOUT,
    startup_timeout=STARTUP_TIMEOUT,
    volumes={PADDLEX_HOME: model_cache},
    secrets=[modal.Secret.from_name("huggingface-secret")],
)
@modal.concurrent(max_inputs=MAX_CONCURRENT)
class OcrService:
    """GPU 容器：主模型常驻，VL 兜底同容器懒加载。"""

    primary: Any
    fallback: Any

    @modal.enter()
    def setup(self) -> None:
        os.makedirs(PADDLEX_HOME, exist_ok=True)
        os.makedirs(HF_HOME, exist_ok=True)
        from paddleocr import PaddleOCR

        logger.info("加载主 OCR 模型 %s（gpu）", PRIMARY_MODEL)
        self.primary = PaddleOCR(
            ocr_version=PRIMARY_MODEL,
            lang=OCR_LANG,
            device="gpu",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
        )
        self.fallback = None
        self._warmup_primary()
        model_cache.commit()

    def _warmup_primary(self) -> None:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (255, 255, 255)).save(buf, format="JPEG")
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(buf.getvalue())
            path = tmp.name
        try:
            self.primary.predict(path)
        finally:
            os.unlink(path)

    def _get_fallback(self) -> Any:
        if self.fallback is not None:
            return self.fallback
        from paddleocr import PaddleOCRVL

        # paddleocr 3.7 本地推理后端名为 native（文档旧称 paddle），T4 不用 vllm-server
        logger.info("加载兜底模型 %s（pipeline_version=v1.6, vl_rec_backend=native）", FALLBACK_MODEL)
        try:
            self.fallback = PaddleOCRVL(
                pipeline_version="v1.6",
                vl_rec_backend="native",
                device="gpu",
            )
        except TypeError as exc:
            raise RuntimeError(
                "当前 paddleocr 不支持 pipeline_version='v1.6'，"
                "拒绝静默回退到默认 PaddleOCR-VL-1.5"
            ) from exc
        model_cache.commit()
        return self.fallback

    def _predict_file(self, engine: Any, image_bytes: bytes, parser) -> list[dict[str, Any]]:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            path = tmp.name
        try:
            results = engine.predict(path)
        finally:
            os.unlink(path)
        return parser(results)

    def _run_primary(self, image_bytes: bytes) -> list[dict[str, Any]]:
        return self._predict_file(self.primary, image_bytes, parse_ppocr_results)

    def _run_vl(self, image_bytes: bytes) -> list[dict[str, Any]]:
        return self._predict_file(self._get_fallback(), image_bytes, parse_vl_results)

    @modal.method()
    def recognize_fallback(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """独立函数：仅低置信或显式指定 PaddleOCR-VL-1.6 时调用。"""
        return self._run_vl(image_bytes)

    def recognize_bytes(self, image_bytes: bytes, model: str) -> list[dict[str, Any]]:
        if model == FALLBACK_MODEL:
            return self._run_vl(image_bytes)
        lines = self._run_primary(image_bytes)
        if not needs_fallback(lines, FALLBACK_THRESHOLD):
            return lines
        logger.info("最低置信度低于 %.2f，调用 %s 兜底", FALLBACK_THRESHOLD, FALLBACK_MODEL)
        try:
            vl_lines = self._run_vl(image_bytes)
        except Exception as exc:
            logger.warning("VL 兜底失败，保留 PP-OCRv6 结果：%s", exc)
            return lines
        if not vl_lines:
            logger.warning("VL 兜底返回空结果，保留 PP-OCRv6 结果")
            return lines
        return vl_lines

    @modal.asgi_app()
    def web(self):
        api = FastAPI(title="ai-comic-review-ocr")

        def _read_jpeg(data: bytes) -> None:
            if not data:
                raise HTTPException(status_code=400, detail="上传文件为空")
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=400, detail="上传文件过大")
            if not data.startswith(JPEG_MAGIC):
                raise HTTPException(status_code=400, detail="仅接受 JPEG 图片")
            try:
                with Image.open(io.BytesIO(data)) as img:
                    img.load()
                    if img.format != "JPEG":
                        raise HTTPException(status_code=400, detail="仅接受 JPEG 图片")
            except HTTPException:
                raise
            except UnidentifiedImageError as exc:
                raise HTTPException(status_code=400, detail="无法解析 JPEG") from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"无法解析 JPEG：{exc}") from exc

        @api.get("/health")
        def health() -> dict:
            return {"status": "ok", "primary": PRIMARY_MODEL, "fallback": FALLBACK_MODEL}

        @api.post("/recognize")
        async def recognize(request: Request) -> dict:
            # 手动解析 multipart，避免嵌套路由里 UploadFile 注解变成 ForwardRef
            content_type = (request.headers.get("content-type") or "").lower()
            if "multipart/form-data" not in content_type:
                raise HTTPException(status_code=400, detail="需要 multipart/form-data")
            form = await request.form()
            model_raw = form.get("model")
            upload = form.get("file")
            if not isinstance(model_raw, str) or not model_raw.strip():
                raise HTTPException(status_code=400, detail="缺少表单字段 model")
            model_id = model_raw.strip()
            if model_id not in ALLOWED_MODELS:
                raise HTTPException(
                    status_code=400,
                    detail=f"不支持的模型标识 '{model_id}'，仅允许 {sorted(ALLOWED_MODELS)}",
                )
            if upload is None:
                raise HTTPException(status_code=400, detail="缺少文件字段 file")
            read = getattr(upload, "read", None)
            if read is None:
                raise HTTPException(status_code=400, detail="file 必须是上传文件")
            data = await read()
            if isinstance(data, str):
                data = data.encode("utf-8")
            _read_jpeg(data)
            try:
                lines = self.recognize_bytes(data, model_id)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=500, detail=f"引擎结果无法映射为契约：{exc}") from exc
            except Exception as exc:
                logger.exception("OCR 识别失败")
                raise HTTPException(status_code=500, detail=f"OCR 识别失败：{exc}") from exc
            return {"lines": lines}

        return api


@app.local_entrypoint()
def main() -> None:
    """本地入口：提示用 HTTPS /recognize 验证契约（见 README.md）。"""
    print(f"App={APP_NAME} GPU={GPU_STR} primary={PRIMARY_MODEL} fallback={FALLBACK_MODEL}")
    print("部署：modal deploy modal/ocr/serve.py")
    print("验证：POST {URL}/recognize  multipart file=JPEG  form model=PP-OCRv6")
