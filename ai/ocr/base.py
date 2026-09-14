"""OCR Provider 抽象：业务层只依赖本模块，不感知具体识别引擎。

更换或新增引擎（如备用疑难 OCR）时实现 OcrProvider 即可，
调用编排（backend/app/services/ocr_pipeline.py）无需改动。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OcrTextLine:
    """单行文字识别结果：文本、文本框四角坐标与置信度（0~1）。"""

    text: str
    bbox: list[list[float]]
    confidence: float


class OcrProvider(ABC):
    """OCR 引擎抽象基类：对单张图片识别文字行列表。"""

    @abstractmethod
    def recognize(self, image_path: Path) -> list[OcrTextLine]:
        """识别图片中的文字，无文字时返回空列表；图片不可读时抛错。"""
        ...
