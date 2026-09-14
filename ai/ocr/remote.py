"""Modal 云端 OCR 服务的 HTTPS 客户端 Provider。

本地不加载 OCR 模型权重，仅把 JPEG 帧以 multipart 上传到 Modal 部署的
OCR 服务并解析返回。服务端点、模型名与超时全部走配置
（core/config.py 的 modal_ocr_url / ocr_primary_model /
ocr_request_timeout_seconds），模型标识不散落在业务代码。

服务契约：POST {MODAL_OCR_URL}/recognize（multipart 字段 file 上传 JPEG，
表单字段 model 携带模型标识）→ JSON：
{"lines": [{"text", "bbox", "confidence"}]}
bbox 为文本框四角坐标 list[list[float]]，confidence 为有限浮点数。
"""

import json
import logging
import math
from pathlib import Path
from typing import Any

import httpx

from ai.ocr.base import OcrProvider, OcrTextLine
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_LINE_KEYS = ("text", "bbox", "confidence")


class OcrProviderError(RuntimeError):
    """OCR Provider 失败的抽象基类；业务层只捕获本层异常，不依赖具体实现模块。"""


class OcrServiceError(OcrProviderError):
    """云端 OCR 服务调用失败（网络错误、超时或非 2xx 响应）。"""


class OcrTimeoutError(OcrServiceError):
    """云端 OCR 服务请求超时。"""


class OcrResponseFormatError(OcrProviderError):
    """云端 OCR 服务返回结构不符合约定（缺字段、类型不符或非有限数值）。"""


class OcrNotConfiguredError(OcrProviderError):
    """未配置 OCR 服务端点，当前无法调用云端识别。"""


__all__ = [
    "OcrNotConfiguredError",
    "OcrProviderError",
    "OcrResponseFormatError",
    "OcrServiceError",
    "OcrTimeoutError",
    "RemoteOcrProvider",
    "parse_recognize_response",
]


def _require_str(value: Any, field: str, context: str) -> str:
    if not isinstance(value, str):
        raise OcrResponseFormatError(
            f"OCR 返回 {context} 的 {field} 字段类型非法：期望 str，实际 {type(value).__name__}"
        )
    return value


def _require_number(value: Any, field: str, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OcrResponseFormatError(
            f"OCR 返回 {context} 的 {field} 字段类型非法：期望数值，实际 {type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise OcrResponseFormatError(
            f"OCR 返回 {context} 的 {field} 字段类型非法：期望有限数值，实际 {value}"
        )
    return number


def _require_bbox(value: Any, context: str) -> list[list[float]]:
    if not isinstance(value, list) or len(value) < 2:
        raise OcrResponseFormatError(
            f"OCR 返回 {context} 的 bbox 字段类型非法：期望至少 2 个点的列表，"
            f"实际 {type(value).__name__}"
        )
    points: list[list[float]] = []
    for index, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise OcrResponseFormatError(
                f"OCR 返回 {context} 的 bbox[{index}] 非法：期望长度为 2 的坐标"
            )
        x = _require_number(point[0], f"bbox[{index}][0]", context)
        y = _require_number(point[1], f"bbox[{index}][1]", context)
        points.append([x, y])
    return points


def parse_recognize_response(data: Any) -> list[OcrTextLine]:
    """把 /recognize 的 JSON 返回解析为文字行列表，结构不符时抛 OcrResponseFormatError。"""
    if not isinstance(data, dict):
        raise OcrResponseFormatError(
            f"OCR 返回结构非法：期望 JSON 对象，实际类型为 {type(data).__name__}"
        )
    if "lines" not in data:
        raise OcrResponseFormatError("OCR 返回缺少 lines 字段")
    lines_raw = data["lines"]
    if not isinstance(lines_raw, list):
        raise OcrResponseFormatError(
            f"OCR 返回 lines 字段类型非法：期望 list，实际 {type(lines_raw).__name__}"
        )

    lines: list[OcrTextLine] = []
    for index, item in enumerate(lines_raw):
        context = f"lines[{index}]"
        if not isinstance(item, dict):
            raise OcrResponseFormatError(
                f"OCR 返回 {context} 类型非法：期望对象，实际 {type(item).__name__}"
            )
        missing = [key for key in _LINE_KEYS if key not in item]
        if missing:
            raise OcrResponseFormatError(
                f"OCR 返回 {context} 缺少字段 {missing}，期望字段为 {_LINE_KEYS}"
            )
        text = _require_str(item["text"], "text", context)
        bbox = _require_bbox(item["bbox"], context)
        confidence = _require_number(item["confidence"], "confidence", context)
        lines.append(OcrTextLine(text=text, bbox=bbox, confidence=confidence))
    return lines


class RemoteOcrProvider(OcrProvider):
    """Modal 云端 OCR 服务的 HTTPS 客户端实现。"""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.modal_ocr_url:
            raise OcrNotConfiguredError(
                "未配置 MODAL_OCR_URL：OCR 推理在 Modal 云端，必须先在 .env 配置服务地址"
            )
        self._model = settings.ocr_primary_model
        # trust_env=False：端点由配置显式指定，不受环境变量代理影响，保证行为确定
        self._client = httpx.Client(
            base_url=settings.modal_ocr_url.rstrip("/"),
            timeout=settings.ocr_request_timeout_seconds,
            trust_env=False,
        )

    def recognize(self, image_path: Path) -> list[OcrTextLine]:
        """上传 JPEG 到云端 OCR 服务并解析文字行。"""
        try:
            with image_path.open("rb") as image_file:
                response = self._client.post(
                    "/recognize",
                    files={"file": (image_path.name, image_file, "image/jpeg")},
                    data={"model": self._model},
                )
        except httpx.TimeoutException as exc:
            raise OcrTimeoutError(
                f"云端 OCR 服务请求超时 image={image_path.name}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OcrServiceError(
                f"云端 OCR 服务请求失败 image={image_path.name}: {exc}"
            ) from exc
        if not response.is_success:
            raise OcrServiceError(
                f"云端 OCR 服务返回非 2xx 状态码={response.status_code} "
                f"image={image_path.name} 响应={response.text[:500]}"
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise OcrResponseFormatError(
                f"云端 OCR 服务返回非 JSON 响应 image={image_path.name}: {exc}"
            ) from exc
        return parse_recognize_response(data)
