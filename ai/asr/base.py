"""ASR Provider 抽象：业务层只依赖本模块，不感知具体识别引擎与部署形态。

引擎实现可以是云端 HTTPS 服务（如 Modal 部署的 Qwen3-ASR）或其他实现，
更换或新增引擎时实现 AsrProvider 即可，
调用编排（backend/app/services/asr_pipeline.py）无需改动。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class AsrProviderError(RuntimeError):
    """ASR Provider 失败的抽象基类；业务层只捕获本层异常，不依赖具体实现模块。"""


class AsrServiceError(AsrProviderError):
    """云端 ASR 服务调用失败（网络错误、超时或非 2xx 响应）。"""


class AsrTimeoutError(AsrServiceError):
    """云端 ASR 服务请求超时。"""


class AsrResponseFormatError(AsrProviderError):
    """云端 ASR 服务返回结构不符合约定（缺字段、类型不符或非有限数值）。"""


class AsrNotConfiguredError(AsrProviderError):
    """未配置 ASR 服务端点，当前无法调用云端识别。"""


@dataclass
class AsrSegment:
    """单段语音识别结果：文本、起止时间（int 毫秒）与置信度（0~1）。"""

    text: str
    start_ms: int
    end_ms: int
    confidence: float


@dataclass
class AsrResult:
    """整段音频的识别结果：按时间升序的分段列表与检测到的语言。"""

    segments: list[AsrSegment]
    language: str


class AsrProvider(ABC):
    """ASR 引擎抽象基类：对整段音频识别并分段返回文本。"""

    @abstractmethod
    def transcribe(self, audio_path: Path) -> AsrResult:
        """识别音频文件，返回分段结果；音频不可读或服务异常时抛错。"""
        ...
