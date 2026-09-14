"""ASR Provider 工厂：默认 Provider 的选择集中在 ai/asr 内部。

业务层（backend/app/services/asr_pipeline.py）只允许依赖 AsrProvider 抽象
与本入口，不得 import 具体引擎实现模块；新增或替换引擎时在此按配置分派，
具体实现模块在函数内延迟导入，未安装引擎依赖时不影响其余功能。
"""

import logging
import threading
from typing import Optional

from ai.asr.base import AsrProvider
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_provider: Optional[AsrProvider] = None
_provider_lock = threading.Lock()


def get_default_provider() -> AsrProvider:
    """返回进程级默认 ASR Provider 单例（线程安全的惰性初始化）。"""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = _create_provider()
    return _provider


def _create_provider() -> AsrProvider:
    """按配置 asr_primary_model 选择并实例化具体引擎实现。"""
    settings = get_settings()
    model = settings.asr_primary_model
    logger.info("初始化 ASR Provider 模型=%s 端点=%s", model, settings.modal_asr_url)
    if model.startswith("Qwen3-ASR"):
        from ai.asr.qwen_asr import QwenAsrProvider

        return QwenAsrProvider()
    raise ValueError(f"未支持的主 ASR 模型标识：{model}")
