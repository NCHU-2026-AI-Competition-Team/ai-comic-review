"""VLM Provider 工厂：默认 Provider 的选择集中在 ai/vlm 内部。

业务层（backend/app/services/vlm_pipeline.py）只允许依赖 VlmProvider 抽象
与本入口，不得 import 具体引擎实现模块；新增或替换引擎时在此按配置分派，
具体实现模块在函数内延迟导入。
"""

import logging
import threading
from typing import Optional

from ai.vlm.base import VlmProvider
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_provider: Optional[VlmProvider] = None
_provider_lock = threading.Lock()


def get_default_provider() -> VlmProvider:
    """返回进程级默认 VLM Provider 单例（线程安全的惰性初始化）。"""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = _create_provider()
    return _provider


def _create_provider() -> VlmProvider:
    """按配置 VLM_PROVIDER 选择并实例化具体引擎实现。"""
    settings = get_settings()
    kind = settings.vlm_provider
    logger.info(
        "初始化 VLM Provider 形态=%s 模型=%s 端点=%s",
        kind,
        settings.vlm_primary,
        settings.modal_vlm_url,
    )
    if kind == "modal":
        model = settings.vlm_primary.strip().lower()
        if model.startswith("qwen3-vl") or model in {"8b", "32b"}:
            from ai.vlm.qwen import QwenVlmProvider

            return QwenVlmProvider()
        raise ValueError(f"未支持的主 VLM 模型标识：{settings.vlm_primary}")
    raise ValueError(f"未支持的 VLM_PROVIDER：{kind}")
