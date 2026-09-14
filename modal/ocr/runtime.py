"""OCR 引擎加载与推理辅助：不依赖 Modal，便于本地单测。

VL 必须用 pipeline_version='v1.6'；可选加速参数不被当前 paddleocr 接受时
才去掉可选项重试，禁止静默落到默认 PaddleOCR-VL-1.5。
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time
from typing import Any, Callable

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

VL_REQUIRED_KWARGS: dict[str, Any] = {
    "pipeline_version": "v1.6",
    "vl_rec_backend": "native",
    "device": "gpu",
}

# 漫画帧不需要文档方向/矫正/图表识别；关掉以免首次 predict 再拉额外权重
VL_OPTIONAL_KWARGS: dict[str, Any] = {
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_chart_recognition": False,
}

_PIPELINE_VERSION_ERROR = (
    "当前 paddleocr 不支持 pipeline_version='v1.6'，拒绝静默回退到默认 PaddleOCR-VL-1.5"
)


def log_step(message: str) -> None:
    """同时打 stdout 与 logger，保证 Modal 应用日志能看到关键耗时。"""
    print(message, flush=True)
    logger.info(message)


def elapsed_seconds(start: float) -> float:
    return time.perf_counter() - start


def vl_engine_kwargs(*, include_optional: bool = True) -> dict[str, Any]:
    kwargs = dict(VL_REQUIRED_KWARGS)
    if include_optional:
        kwargs.update(VL_OPTIONAL_KWARGS)
    return kwargs


def dummy_jpeg_bytes(
    width: int = 320,
    height: int = 80,
    text: str = "HELLO",
) -> bytes:
    """生成带字 JPEG，用于 warmup，走通 VL 的 generate 路径。"""
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text((10, max(0, height // 2 - 10)), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


def _is_unexpected_kwarg(exc: TypeError, name: str) -> bool:
    msg = str(exc).lower()
    return name.lower() in msg and "unexpected" in msg


def create_vl_engine(paddleocr_vl_cls: Any) -> Any:
    """构造 PaddleOCR-VL-1.6；可选 kwargs 不支持时降级，pipeline_version 不支持则失败。"""
    required = vl_engine_kwargs(include_optional=False)
    optional = dict(VL_OPTIONAL_KWARGS)
    try:
        return paddleocr_vl_cls(**required, **optional)
    except TypeError as exc:
        if _is_unexpected_kwarg(exc, "pipeline_version"):
            raise RuntimeError(_PIPELINE_VERSION_ERROR) from exc
        log_step(f"VL 可选初始化参数不受支持，回退为必填参数：{exc}")
        try:
            return paddleocr_vl_cls(**required)
        except TypeError as exc2:
            if _is_unexpected_kwarg(exc2, "pipeline_version"):
                raise RuntimeError(_PIPELINE_VERSION_ERROR) from exc2
            raise


def predict_image(engine: Any, image_bytes: bytes) -> Any:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        path = tmp.name
    try:
        return engine.predict(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            log_step(f"临时 JPEG 删除失败：{path}")


def warmup_engine(engine: Any, image_bytes: bytes | None = None) -> None:
    payload = dummy_jpeg_bytes() if image_bytes is None else image_bytes
    predict_image(engine, payload)


def parse_predict(engine: Any, image_bytes: bytes, parser: Callable[[Any], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    started = time.perf_counter()
    results = predict_image(engine, image_bytes)
    log_step(f"引擎 predict 完成，耗时 {elapsed_seconds(started):.1f}s")
    return parser(results)
