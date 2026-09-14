"""PaddleOCR Provider 实现：主 OCR 引擎（PP-OCRv6）。

模型名、语言与设备全部走配置（core/config.py 的 ocr_* 项），
模型标识不散落在业务代码；引擎实例由 ai.ocr.factory 以惰性单例持有，
首次调用时才初始化并加载模型（权重首次自动下载到用户缓存目录，
不进入仓库）。备用疑难 OCR（PaddleOCR-VL）后续实现同接口接入，
其模型标识由配置 ocr_fallback_model 记录。
"""

import logging
from pathlib import Path
from typing import Any, Iterable

from ai.ocr.base import OcrProvider, OcrTextLine
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 结果解析兼容的结构版本：PaddleOCR 3.x（PP-OCRv6）predict() 返回的
# dict-like 结果，含 rec_texts / rec_scores / rec_polys 三个字段；
# 旧版 ocr() 的嵌套 [[box, (text, score)]] 结构不在兼容范围，明确报错
_RESULT_KEYS = ("rec_texts", "rec_scores", "rec_polys")


class OcrResultFormatError(RuntimeError):
    """引擎返回结构不符合预期（非 PP-OCRv6 predict 的 dict-like 结构）。"""


def parse_predict_results(results: Iterable[Any]) -> list[OcrTextLine]:
    """把 PaddleOCR predict() 的返回解析为文字行列表，结构不符时抛 OcrResultFormatError。"""
    lines: list[OcrTextLine] = []
    for result in results:
        lines.extend(_parse_single_result(result))
    return lines


def _parse_single_result(result: Any) -> list[OcrTextLine]:
    if isinstance(result, (list, tuple)):
        raise OcrResultFormatError(
            "不支持的 OCR 返回结构：疑似旧版嵌套 [[box, (text, score)]] 列表，"
            "当前实现仅兼容 PaddleOCR 3.x predict() 的 dict-like 结果"
        )
    try:
        keys = result.keys()
    except AttributeError:
        raise OcrResultFormatError(
            f"不支持的 OCR 返回结构：期望含 {_RESULT_KEYS} 的 dict-like 结果，"
            f"实际类型为 {type(result).__name__}"
        ) from None
    missing = [key for key in _RESULT_KEYS if key not in keys]
    if missing:
        raise OcrResultFormatError(
            f"OCR 返回结果缺少字段 {missing}，期望字段为 {_RESULT_KEYS}，"
            "可能是 PaddleOCR 版本与当前实现不兼容"
        )

    texts = result["rec_texts"]
    scores = result["rec_scores"]
    polys = result["rec_polys"]
    if not (len(texts) == len(scores) == len(polys)):
        raise OcrResultFormatError(
            "OCR 返回结果字段长度不一致："
            f"rec_texts={len(texts)} rec_scores={len(scores)} rec_polys={len(polys)}"
        )

    lines = []
    for text, score, poly in zip(texts, scores, polys):
        bbox = poly.tolist() if hasattr(poly, "tolist") else [list(p) for p in poly]
        lines.append(OcrTextLine(text=text, bbox=bbox, confidence=float(score)))
    return lines


class PaddleOcrProvider(OcrProvider):
    """基于 PaddleOCR 官方包的 OCR 实现（默认 PP-OCRv6，CPU）。"""

    def __init__(self) -> None:
        # 必须先于 paddle 加载 torch：Windows 下 paddle 先加载会把冲突版本的
        # DLL 装入进程，导致随后 torch 的 shm.dll 解析失败（WinError 127），
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
        return parse_predict_results(results)
