"""OCR 云端服务结果映射：把引擎输出转成 OcrTextLine JSON 契约。

本模块不依赖 Modal / FastAPI，便于本地单测。
"""

from __future__ import annotations

from typing import Any


def _as_mapping(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    try:
        return {key: result[key] for key in result.keys()}
    except Exception as exc:
        raise ValueError(f"OCR 引擎返回非 dict-like 结果：{type(result).__name__}") from exc


def _bbox_to_quad(poly: Any) -> list[list[float]]:
    """把引擎多边形/xyxy 转成 OcrTextLine 的四角坐标 [[x,y], ...]。"""
    if hasattr(poly, "tolist"):
        poly = poly.tolist()
    if not isinstance(poly, (list, tuple)) or not poly:
        raise ValueError("bbox 为空")
    first = poly[0]
    if isinstance(first, (int, float)):
        if len(poly) != 4:
            raise ValueError("xyxy bbox 长度必须为 4")
        x1, y1, x2, y2 = (float(poly[0]), float(poly[1]), float(poly[2]), float(poly[3]))
        return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    points: list[list[float]] = []
    for point in poly:
        if hasattr(point, "tolist"):
            point = point.tolist()
        points.append([float(point[0]), float(point[1])])
    if len(points) < 2:
        raise ValueError("bbox 点数不足")
    return points


def _line_dict(text: str, bbox: list[list[float]], confidence: float) -> dict[str, Any]:
    return {
        "text": str(text),
        "bbox": [[float(x), float(y)] for x, y in bbox],
        "confidence": float(confidence),
    }


def parse_ppocr_results(results: Any) -> list[dict[str, Any]]:
    """解析 PP-OCRv6 predict() 的 rec_texts/rec_scores/rec_polys 结构。"""
    if results is None:
        return []
    if not isinstance(results, (list, tuple)):
        results = [results]
    lines: list[dict[str, Any]] = []
    for result in results:
        data = _as_mapping(result)
        missing = [key for key in ("rec_texts", "rec_scores", "rec_polys") if key not in data]
        if missing:
            raise ValueError(f"PP-OCRv6 返回缺少字段 {missing}")
        texts = list(data["rec_texts"])
        scores = list(data["rec_scores"])
        polys = list(data["rec_polys"])
        if not (len(texts) == len(scores) == len(polys)):
            raise ValueError(
                "PP-OCRv6 返回字段长度不一致："
                f"rec_texts={len(texts)} rec_scores={len(scores)} rec_polys={len(polys)}"
            )
        for text, score, poly in zip(texts, scores, polys):
            content = str(text).strip()
            if not content:
                continue
            lines.append(_line_dict(content, _bbox_to_quad(poly), float(score)))
    return lines


def _is_vl_block(obj: Any) -> bool:
    """paddleocr 3.7 的 PaddleOCRVLBlock：属性 content/bbox，没有 keys()。"""
    return hasattr(obj, "content") and hasattr(obj, "bbox") and not hasattr(obj, "keys")


def _block_to_line(block: Any) -> dict[str, Any] | None:
    text = str(getattr(block, "content", "") or "").strip()
    if not text:
        return None
    bbox_raw = getattr(block, "bbox", None)
    if bbox_raw is None:
        raise ValueError("PaddleOCR-VL 返回块缺少 bbox")
    score = getattr(block, "score", 1.0)
    if score is None:
        score = 1.0
    return _line_dict(text, _bbox_to_quad(bbox_raw), float(score))


def parse_vl_results(results: Any) -> list[dict[str, Any]]:
    """解析 PaddleOCR-VL 的 parsing_res_list，映射为 OcrTextLine JSON。

    VL 文档解析结果通常不含逐行置信度；缺失时填 1.0，与契约字段对齐。
    paddleocr 3.7 可能直接返回 PaddleOCRVLBlock 列表，或 dict-like 结果含 parsing_res_list。
    若结果同时带 rec_texts（部分版本），优先走 PP-OCR 结构以保留真实置信度。
    """
    if results is None:
        return []
    if not isinstance(results, (list, tuple)):
        results = [results]
    lines: list[dict[str, Any]] = []
    for result in results:
        if _is_vl_block(result):
            line = _block_to_line(result)
            if line is not None:
                lines.append(line)
            continue
        data = _as_mapping(result)
        if all(key in data for key in ("rec_texts", "rec_scores", "rec_polys")):
            lines.extend(parse_ppocr_results([data]))
            continue
        blocks = data.get("parsing_res_list") or []
        for block in blocks:
            if _is_vl_block(block):
                line = _block_to_line(block)
                if line is not None:
                    lines.append(line)
                continue
            block_data = _as_mapping(block) if not isinstance(block, dict) else block
            text = str(
                block_data.get("block_content")
                or block_data.get("content")
                or ""
            ).strip()
            if not text:
                continue
            bbox_raw = block_data.get("block_bbox") or block_data.get("bbox")
            if bbox_raw is None:
                raise ValueError("PaddleOCR-VL 返回块缺少 bbox")
            score = block_data.get("block_score", block_data.get("score", 1.0))
            lines.append(_line_dict(text, _bbox_to_quad(bbox_raw), float(score)))
    return lines


def needs_fallback(lines: list[dict[str, Any]], threshold: float) -> bool:
    """仅在已检出文字且最低置信度低于阈值时触发 VL 兜底；空结果不调用 VL。"""
    if not lines:
        return False
    return min(float(line["confidence"]) for line in lines) < threshold
