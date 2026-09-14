"""Qwen3-ASR Provider 实现：Modal 云端 ASR 服务的 HTTPS 客户端。

本地不加载任何模型权重，仅把提取好的 wav 以 multipart 上传到
Modal 部署的 ASR 服务并解析返回。服务端点、模型名与超时全部走配置
（core/config.py 的 modal_asr_url / asr_primary_model /
asr_request_timeout_seconds），模型标识不散落在业务代码。

服务契约：POST {MODAL_ASR_URL}/transcribe（multipart 字段 file 上传 wav，
表单字段 model 携带模型标识）→ JSON：
{"segments": [{"text", "start_ms", "end_ms", "confidence"}], "language": "zh"}
"""

import json
import logging
import math
from pathlib import Path
from typing import Any

import httpx

from ai.asr.base import (
    AsrNotConfiguredError,
    AsrProvider,
    AsrProviderError,
    AsrResponseFormatError,
    AsrResult,
    AsrSegment,
    AsrServiceError,
    AsrTimeoutError,
)
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_SEGMENT_KEYS = ("text", "start_ms", "end_ms", "confidence")

# 向后兼容：既有测试与调用方可继续从本模块引用抽象异常名
__all__ = [
    "AsrNotConfiguredError",
    "AsrProviderError",
    "AsrResponseFormatError",
    "AsrServiceError",
    "AsrTimeoutError",
    "QwenAsrProvider",
    "parse_transcribe_response",
]


def _require_str(value: Any, field: str, context: str) -> str:
    if not isinstance(value, str):
        raise AsrResponseFormatError(
            f"ASR 返回 {context} 的 {field} 字段类型非法：期望 str，实际 {type(value).__name__}"
        )
    return value


def _require_number(value: Any, field: str, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AsrResponseFormatError(
            f"ASR 返回 {context} 的 {field} 字段类型非法：期望数值，实际 {type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise AsrResponseFormatError(
            f"ASR 返回 {context} 的 {field} 字段类型非法：期望有限数值，实际 {value}"
        )
    return number


def parse_transcribe_response(data: Any) -> AsrResult:
    """把 /transcribe 的 JSON 返回解析为 AsrResult，结构不符时抛 AsrResponseFormatError。"""
    if not isinstance(data, dict):
        raise AsrResponseFormatError(
            f"ASR 返回结构非法：期望 JSON 对象，实际类型为 {type(data).__name__}"
        )
    if "segments" not in data:
        raise AsrResponseFormatError("ASR 返回缺少 segments 字段")
    if "language" not in data:
        raise AsrResponseFormatError("ASR 返回缺少 language 字段")

    segments_raw = data["segments"]
    if not isinstance(segments_raw, list):
        raise AsrResponseFormatError(
            f"ASR 返回 segments 字段类型非法：期望 list，实际 {type(segments_raw).__name__}"
        )
    language = _require_str(data["language"], "language", "结果")

    segments = []
    for index, item in enumerate(segments_raw):
        context = f"segments[{index}]"
        if not isinstance(item, dict):
            raise AsrResponseFormatError(
                f"ASR 返回 {context} 类型非法：期望对象，实际 {type(item).__name__}"
            )
        missing = [key for key in _SEGMENT_KEYS if key not in item]
        if missing:
            raise AsrResponseFormatError(
                f"ASR 返回 {context} 缺少字段 {missing}，期望字段为 {_SEGMENT_KEYS}"
            )
        text = _require_str(item["text"], "text", context)
        start_ms = round(_require_number(item["start_ms"], "start_ms", context))
        end_ms = round(_require_number(item["end_ms"], "end_ms", context))
        confidence = _require_number(item["confidence"], "confidence", context)
        if start_ms < 0 or end_ms < start_ms:
            raise AsrResponseFormatError(
                f"ASR 返回 {context} 时间区间非法：start_ms={start_ms} end_ms={end_ms}"
            )
        segments.append(
            AsrSegment(text=text, start_ms=start_ms, end_ms=end_ms, confidence=confidence)
        )
    return AsrResult(segments=segments, language=language)


class QwenAsrProvider(AsrProvider):
    """Modal 云端 Qwen3-ASR 服务的 HTTPS 客户端实现。"""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.modal_asr_url:
            raise AsrNotConfiguredError(
                "未配置 MODAL_ASR_URL：ASR 推理在 Modal 云端，必须先在 .env 配置服务地址"
            )
        self._model = settings.asr_primary_model
        # trust_env=False：端点由配置显式指定，不受环境变量代理影响，保证行为确定
        self._client = httpx.Client(
            base_url=settings.modal_asr_url.rstrip("/"),
            timeout=settings.asr_request_timeout_seconds,
            trust_env=False,
        )

    def transcribe(self, audio_path: Path) -> AsrResult:
        """上传 wav 到云端 ASR 服务并解析分段结果。"""
        try:
            with audio_path.open("rb") as audio_file:
                response = self._client.post(
                    "/transcribe",
                    files={"file": (audio_path.name, audio_file, "audio/wav")},
                    data={"model": self._model},
                )
        except httpx.TimeoutException as exc:
            raise AsrTimeoutError(
                f"云端 ASR 服务请求超时 audio={audio_path.name}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AsrServiceError(
                f"云端 ASR 服务请求失败 audio={audio_path.name}: {exc}"
            ) from exc
        if not response.is_success:
            raise AsrServiceError(
                f"云端 ASR 服务返回非 2xx 状态码={response.status_code} "
                f"audio={audio_path.name} 响应={response.text[:500]}"
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise AsrResponseFormatError(
                f"云端 ASR 服务返回非 JSON 响应 audio={audio_path.name}: {exc}"
            ) from exc
        return parse_transcribe_response(data)
