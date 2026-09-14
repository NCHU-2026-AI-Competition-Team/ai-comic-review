"""OCR 引擎返回结果解析的单元测试。

只测 parse_predict_results 纯函数（不实例化引擎，无需 paddleocr/torch），
覆盖当前 PP-OCRv6 dict-like 结构的正常解析，以及旧版嵌套结构、缺字段、
长度不一致等异常结构的明确报错。
"""

import pytest

from ai.ocr.paddleocr import OcrResultFormatError, parse_predict_results


def test_parse_current_dict_like_structure() -> None:
    """PP-OCRv6 predict() 的 dict-like 结果（rec_texts/rec_scores/rec_polys）正常解析。"""

    class FakePoly:
        def __init__(self, points: list[list[float]]) -> None:
            self._points = points

        def tolist(self) -> list[list[float]]:
            return self._points

    results = [
        {
            "rec_texts": ["你好", "世界"],
            "rec_scores": [0.95, 0.87],
            "rec_polys": [
                FakePoly([[0, 0], [10, 0], [10, 10], [0, 10]]),
                [[0, 20], [10, 20], [10, 30], [0, 30]],
            ],
        }
    ]
    lines = parse_predict_results(results)

    assert len(lines) == 2
    assert lines[0].text == "你好"
    assert lines[0].confidence == pytest.approx(0.95)
    assert lines[0].bbox == [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert lines[1].text == "世界"
    assert lines[1].bbox == [[0, 20], [10, 20], [10, 30], [0, 30]]


def test_parse_empty_results_returns_empty() -> None:
    """引擎无检出（空列表 / 空字段）时返回空列表。"""
    assert parse_predict_results([]) == []
    assert parse_predict_results([{"rec_texts": [], "rec_scores": [], "rec_polys": []}]) == []


def test_parse_legacy_nested_structure_raises() -> None:
    """旧版 ocr() 的嵌套 [[box, (text, score)]] 结构明确报错而非静默错解析。"""
    legacy = [[[[[0, 0], [10, 0], [10, 10], [0, 10]], ("你好", 0.9)]]]
    with pytest.raises(OcrResultFormatError, match="旧版嵌套"):
        parse_predict_results(legacy)


def test_parse_missing_fields_raises() -> None:
    """缺少 rec_polys 等必需字段时明确报错并指出缺失字段。"""
    with pytest.raises(OcrResultFormatError, match="rec_polys"):
        parse_predict_results([{"rec_texts": ["你好"], "rec_scores": [0.9]}])


def test_parse_non_dict_like_raises() -> None:
    """非 dict-like 结果（如字符串）明确报错并指出实际类型。"""
    with pytest.raises(OcrResultFormatError, match="str"):
        parse_predict_results(["意外字符串"])


def test_parse_inconsistent_field_lengths_raises() -> None:
    """rec_texts/rec_scores/rec_polys 长度不一致时明确报错。"""
    with pytest.raises(OcrResultFormatError, match="长度不一致"):
        parse_predict_results(
            [{"rec_texts": ["你好", "世界"], "rec_scores": [0.9], "rec_polys": [[[0, 0]]]}]
        )
