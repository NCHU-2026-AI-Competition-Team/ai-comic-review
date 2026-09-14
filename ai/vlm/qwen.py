"""Qwen3-VL Provider 实现：Modal 云端 VLM 主审服务的 HTTPS 客户端。

本地不加载任何模型权重，仅把 JPEG 多帧以 multipart 上传到 Modal 部署的
VLM 服务并解析返回。服务端点、模型名与超时全部走配置
（core/config.py 的 modal_vlm_url / vlm_primary /
vlm_request_timeout_seconds），模型标识不散落在业务代码。

服务契约：POST {MODAL_VLM_URL}/review
multipart 多文件字段 frames（JPEG）+ 表单字段
model / ocr_text / asr_text / context / rules / frame_timestamps_ms
→ 严格 JSON：{risk, category, severity, confidence, start_ms, end_ms,
evidence, reason, suggestion}
model 传 32B 标识时云端返回 501（本期未部署）。
"""

import json
import logging
from typing import Any

import httpx

from ai.vlm.base import (
    VlmEscalationUnavailableError,
    VlmNotConfiguredError,
    VlmProvider,
    VlmProviderError,
    VlmResponseFormatError,
    VlmReviewInput,
    VlmServiceError,
    VlmTimeoutError,
)
from ai.vlm.schemas import VlmReviewResult, parse_review_response
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 向后兼容：调用方可从本模块引用抽象异常名
__all__ = [
    "QwenVlmProvider",
    "VlmEscalationUnavailableError",
    "VlmNotConfiguredError",
    "VlmProviderError",
    "VlmResponseFormatError",
    "VlmServiceError",
    "VlmTimeoutError",
    "parse_review_response",
]


_RETRYABLE_STATUS = {502, 503, 504}


class QwenVlmProvider(VlmProvider):
    """Modal 云端 Qwen3-VL 主审服务的 HTTPS 客户端实现。"""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.modal_vlm_url:
            raise VlmNotConfiguredError(
                "未配置 MODAL_VLM_URL：VLM 推理在 Modal 云端，必须先在 .env 配置服务地址"
            )
        self._model = settings.vlm_primary
        self._max_retries = settings.vlm_request_max_retries
        self._max_frames = settings.vlm_max_frames_per_request
        # trust_env=False：端点由配置显式指定，不受环境变量代理影响，保证行为确定
        self._client = httpx.Client(
            base_url=settings.modal_vlm_url.rstrip("/"),
            timeout=settings.vlm_request_timeout_seconds,
            trust_env=False,
        )

    def review(self, payload: VlmReviewInput) -> VlmReviewResult:
        """上传多帧 JPEG 到云端 /review 并解析严格 JSON 结果。"""
        if not payload.frame_paths:
            raise VlmServiceError("VLM 主审请求缺少帧图片")
        if len(payload.frame_paths) != len(payload.frame_timestamps_ms):
            raise VlmServiceError(
                f"帧数量与时间戳数量不一致：frames={len(payload.frame_paths)} "
                f"timestamps={len(payload.frame_timestamps_ms)}"
            )
        if len(payload.frame_paths) > self._max_frames:
            raise VlmServiceError(
                f"单次主审最多 {self._max_frames} 帧，实际 {len(payload.frame_paths)} 帧"
            )

        model = payload.model or self._model
        form: dict[str, str] = {
            "model": model,
            "ocr_text": payload.ocr_text,
            "asr_text": payload.asr_text,
            "context": payload.context,
            "rules": payload.rules,
            "frame_timestamps_ms": json.dumps(list(payload.frame_timestamps_ms)),
        }
        attempts = self._max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._post_review(payload, form)
            except VlmTimeoutError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    logger.warning("云端 VLM 请求超时，准备重试 attempt=%d/%d", attempt + 1, attempts)
                    continue
                raise
            except VlmServiceError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    logger.warning("云端 VLM 请求失败，准备重试 attempt=%d/%d: %s", attempt + 1, attempts, exc)
                    continue
                raise

            if response.status_code == 501:
                raise VlmEscalationUnavailableError(
                    f"云端 VLM 复审模型未部署 status=501 model={model} "
                    f"响应={response.text[:500]}"
                )
            if response.status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
                logger.warning(
                    "云端 VLM 返回 %s，准备重试 attempt=%d/%d",
                    response.status_code,
                    attempt + 1,
                    attempts,
                )
                last_error = VlmServiceError(
                    f"云端 VLM 服务返回非 2xx 状态码={response.status_code} 响应={response.text[:500]}"
                )
                continue
            if not response.is_success:
                raise VlmServiceError(
                    f"云端 VLM 服务返回非 2xx 状态码={response.status_code} "
                    f"frames={len(payload.frame_paths)} 响应={response.text[:500]}"
                )
            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                raise VlmResponseFormatError(
                    f"云端 VLM 服务返回非 JSON 响应: {exc}"
                ) from exc
            return parse_review_response(data)

        raise VlmServiceError(f"云端 VLM 服务重试耗尽: {last_error}")

    def _post_review(self, payload: VlmReviewInput, form: dict[str, str]) -> httpx.Response:
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for path in payload.frame_paths:
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise VlmServiceError(f"无法读取帧图片 {path.name}: {exc}") from exc
            files.append(("frames", (path.name, content, "image/jpeg")))
        try:
            return self._client.post("/review", files=files, data=form)
        except httpx.TimeoutException as exc:
            names = ",".join(path.name for path in payload.frame_paths)
            raise VlmTimeoutError(f"云端 VLM 服务请求超时 frames={names}: {exc}") from exc
        except httpx.HTTPError as exc:
            names = ",".join(path.name for path in payload.frame_paths)
            raise VlmServiceError(f"云端 VLM 服务请求失败 frames={names}: {exc}") from exc
