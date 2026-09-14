"""PaddleOCR Provider 实现：主 OCR 引擎（PP-OCRv6）。

模型名、语言与设备全部走配置（core/config.py 的 ocr_* 项），
模型标识不散落在业务代码；引擎为模块级惰性单例，首次调用
get_provider 时才初始化并加载模型（权重首次自动下载到用户缓存目录，
不进入仓库）。备用疑难 OCR（PaddleOCR-VL）后续实现同接口接入，
其模型标识由配置 ocr_fallback_model 记录。
"""

import logging
from pathlib import Path
from typing import Optional

from ai.ocr.base import OcrProvider, OcrTextLine
from app.core.config import get_settings

logger = logging.getLogger(__name__)


class PaddleOcrProvider(OcrProvider):
    """基于 PaddleOCR 官方包的 OCR 实现（默认 PP-OCRv6，CPU）。"""

    def __init__(self) -> None:
        # 必须先于 paddle 加载 torch：paddle 先加载会把冲突版本的 DLL
        # 装入进程，导致随后 torch 的 shm.dll 解析失败（WinError 127），
        # 进而拖垮 modelscope → paddlex → paddleocr 整条导入链
        import torch  # noqa: F401
        # 延迟导入：保证未安装 OCR 依赖时后端其余功能不受影响
        from paddleocr import PaddleOCR

        settings = get_settings()
        self._engine = PaddleOCR(
            ocr_version=settings.ocr_primary_model,
            lang=settings.ocr_lang,
            device="gpu" if settings.ocr_use_gpu else "cpu",
            # 文档方向分类与去畸变面向扫描文档，对视频帧无意义，关闭以减少模型下载与耗时
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
        )

    def recognize(self, image_path: Path) -> list[OcrTextLine]:
        """识别单张图片，返回文字行列表；未检出文字时返回空列表。"""
        results = self._engine.predict(str(image_path))
        lines = []
        for result in results:
            texts = result["rec_texts"]
            scores = result["rec_scores"]
            polys = result["rec_polys"]
            for text, score, poly in zip(texts, scores, polys):
                bbox = poly.tolist() if hasattr(poly, "tolist") else [list(p) for p in poly]
                lines.append(
                    OcrTextLine(text=text, bbox=bbox, confidence=float(score))
                )
        return lines


_provider: Optional[OcrProvider] = None


def get_provider() -> OcrProvider:
    """返回进程级 OCR Provider 单例，首次调用时才初始化并加载模型。"""
    global _provider
    if _provider is None:
        settings = get_settings()
        logger.info(
            "初始化 OCR Provider 模型=%s 语言=%s 设备=%s（首次调用加载模型）",
            settings.ocr_primary_model,
            settings.ocr_lang,
            "gpu" if settings.ocr_use_gpu else "cpu",
        )
        _provider = PaddleOcrProvider()
    return _provider
