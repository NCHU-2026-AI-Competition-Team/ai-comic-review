"""VLM Provider 抽象：业务层只依赖本模块，不感知具体引擎与部署形态。

引擎实现可以是云端 HTTPS 服务（如 Modal 部署的 Qwen3-VL）或其他实现，
更换或新增引擎时实现 VlmProvider 即可，
调用编排（backend/app/services/vlm_pipeline.py）无需改动。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai.vlm.schemas import VlmReviewResult


class VlmProviderError(RuntimeError):
    """VLM Provider 失败的抽象基类；业务层只捕获本层异常，不依赖具体实现模块。"""


class VlmServiceError(VlmProviderError):
    """云端 VLM 服务调用失败（网络错误、超时或非 2xx 响应）。"""


class VlmTimeoutError(VlmServiceError):
    """云端 VLM 服务请求超时。"""


class VlmResponseFormatError(VlmProviderError):
    """云端 VLM 服务返回结构不符合约定（缺字段、类型不符或取值非法）。"""


class VlmNotConfiguredError(VlmProviderError):
    """未配置 VLM 服务端点，当前无法调用云端主审。"""


class VlmEscalationUnavailableError(VlmProviderError):
    """复审模型（escalation / 32B）未部署，服务返回 501。"""


@dataclass
class VlmReviewInput:
    """一次主审请求：多帧图片 + 文本上下文，字段与云端 /review 契约对齐。"""

    frame_paths: list[Path]
    frame_timestamps_ms: list[int]
    ocr_text: str = ""
    asr_text: str = ""
    context: str = ""
    rules: str = ""
    model: str | None = None


class VlmProvider(ABC):
    """VLM 引擎抽象基类：对一组关键帧 + 文本上下文做内容安全审核。"""

    @abstractmethod
    def review(self, payload: VlmReviewInput) -> "VlmReviewResult":
        """执行一次主审并返回严格 JSON 契约对应的结果；服务异常时抛错。"""
        ...
