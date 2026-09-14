"""OCR Provider 工厂：默认 Provider 的选择集中在 ai/ocr 内部。

业务层（backend/app/services/ocr_pipeline.py）只允许依赖 OcrProvider 抽象
与本入口，不得 import 具体引擎实现模块；新增或替换引擎时在此按配置分派，
具体实现模块在函数内延迟导入，未安装引擎依赖时不影响其余功能。
"""

import logging
import threading
from typing import Optional

from ai.ocr.base import OcrProvider
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_provider: Optional[OcrProvider] = None
_provider_lock = threading.Lock()


def get_default_provider() -> OcrProvider:
    """返回进程级默认 OCR Provider 单例（线程安全的惰性初始化）。

    多请求并发首次触发时以锁保证只初始化一次，避免重复加载模型。
    """
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = _create_provider()
    return _provider


def _create_provider() -> OcrProvider:
    """按配置 ocr_primary_model 选择并实例化具体引擎实现。"""
    settings = get_settings()
    model = settings.ocr_primary_model
    logger.info(
        "初始化 OCR Provider 模型=%s 语言=%s 设备=%s（首次调用加载模型）",
        model,
        settings.ocr_lang,
        "gpu" if settings.ocr_use_gpu else "cpu",
    )
    if model.startswith("PP-OCR"):
        from ai.ocr.paddleocr import PaddleOcrProvider

        return PaddleOcrProvider()
    raise ValueError(f"未支持的主 OCR 模型标识：{model}")
