"""modal/vlm/review_contract.py 的纯逻辑测试：模型路由、输入校验、输出契约。

review_contract.py 仅依赖标准库；为避免在 pytest 环境用本地 modal/ 目录
遮蔽 Modal SDK 包，这里按文件路径加载模块，不走包导入。
"""

import importlib.util
import json
from pathlib import Path

import pytest

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "modal" / "vlm" / "review_contract.py"

_spec = importlib.util.spec_from_file_location("modal_vlm_review_contract", _CONTRACT_PATH)
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)


def _safe_result(**overrides):
    result = {
        "risk": False,
        "category": "none",
        "severity": "none",
        "confidence": 0.95,
        "start_ms": 0,
        "end_ms": 0,
        "evidence": "画面为普通室内场景",
        "reason": "未发现违规内容",
        "suggestion": "通过",
    }
    result.update(overrides)
    return result


# ---------------------------------------------------------------------------
# 模型路由：主审 8B / escalation 32B 预留 / 未知标识
# ---------------------------------------------------------------------------


class TestResolveModelRole:
    def test_empty_defaults_to_primary(self):
        assert contract.resolve_model_role("") == contract.MODEL_ROLE_PRIMARY
        assert contract.resolve_model_role(None) == contract.MODEL_ROLE_PRIMARY

    @pytest.mark.parametrize("alias", ["qwen3-vl-8b-instruct", "Qwen3-VL-8B", "8b", " 8B "])
    def test_primary_aliases(self, alias):
        assert contract.resolve_model_role(alias) == contract.MODEL_ROLE_PRIMARY

    @pytest.mark.parametrize("alias", ["qwen3-vl-32b-instruct", "qwen3-vl-32b", "32b"])
    def test_escalation_aliases_reserved(self, alias):
        # 32B 复审模型本期不部署，但必须被识别为 escalation 以返回 501 而非 400
        assert contract.resolve_model_role(alias) == contract.MODEL_ROLE_ESCALATION

    def test_unknown_model_raises(self):
        with pytest.raises(contract.UnknownModelError):
            contract.resolve_model_role("qwen2-vl-7b")


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_text_field_within_limit(self):
        contract.validate_text_fields({"ocr_text": "x" * 100})

    def test_text_field_over_limit(self):
        with pytest.raises(contract.InputValidationError):
            contract.validate_text_fields({"ocr_text": "x" * (contract.MAX_TEXT_FIELD_CHARS + 1)})

    def test_timestamps_default_axis(self):
        assert contract.parse_frame_timestamps("", 3) == [0, 500, 1000]

    def test_timestamps_parse(self):
        assert contract.parse_frame_timestamps("[0, 500, 1200]", 3) == [0, 500, 1200]

    def test_timestamps_length_mismatch(self):
        with pytest.raises(contract.InputValidationError):
            contract.parse_frame_timestamps("[0, 500]", 3)

    def test_timestamps_invalid_json(self):
        with pytest.raises(contract.InputValidationError):
            contract.parse_frame_timestamps("not-json", 2)

    def test_timestamps_negative_or_bool(self):
        with pytest.raises(contract.InputValidationError):
            contract.parse_frame_timestamps("[0, -1]", 2)
        with pytest.raises(contract.InputValidationError):
            contract.parse_frame_timestamps("[0, true]", 2)


# ---------------------------------------------------------------------------
# prompt 构建
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_prompt_contains_context_and_timeline(self):
        prompt = contract.build_review_prompt(
            [0, 500], ocr_text="字幕文字", asr_text="台词", context="漫剧第3集", rules="严查血腥"
        )
        assert "第1帧(t=0ms)" in prompt and "第2帧(t=500ms)" in prompt
        assert "字幕文字" in prompt and "台词" in prompt
        assert "漫剧第3集" in prompt and "严查血腥" in prompt
        for field in contract.REVIEW_RESULT_FIELDS:
            assert f'"{field}"' in prompt

    def test_prompt_defaults_when_empty(self):
        prompt = contract.build_review_prompt([0])
        assert "（无）" in prompt
        assert "通用内容安全规范" in prompt


# ---------------------------------------------------------------------------
# 输出契约：JSON Schema 常量与校验归一化
# ---------------------------------------------------------------------------


class TestJsonSchema:
    def test_schema_required_matches_fields(self):
        assert set(contract.REVIEW_JSON_SCHEMA["required"]) == set(contract.REVIEW_RESULT_FIELDS)
        assert set(contract.REVIEW_JSON_SCHEMA["properties"]) == set(contract.REVIEW_RESULT_FIELDS)

    def test_schema_enums_match_constants(self):
        props = contract.REVIEW_JSON_SCHEMA["properties"]
        assert props["category"]["enum"] == list(contract.RISK_CATEGORIES)
        assert props["severity"]["enum"] == list(contract.SEVERITY_LEVELS)
        assert props["start_ms"]["type"] == "integer"
        assert props["end_ms"]["type"] == "integer"
        assert props["confidence"]["minimum"] == 0.0
        assert props["confidence"]["maximum"] == 1.0
        assert contract.REVIEW_JSON_SCHEMA["additionalProperties"] is False


class TestValidateReviewResult:
    def test_safe_result_normalized(self):
        result = contract.validate_review_result(
            _safe_result(category="porn", severity="low", start_ms=100, end_ms=200)
        )
        # risk=false 时强制约定空值，时间归零
        assert result["risk"] is False
        assert result["category"] == "none"
        assert result["severity"] == "none"
        assert result["start_ms"] == 0
        assert result["end_ms"] == 0

    def test_risky_result_passes(self):
        result = contract.validate_review_result(
            _safe_result(risk=True, category="blood", severity="high", start_ms=500, end_ms=1500)
        )
        assert result["risk"] is True
        assert result["category"] == "blood"
        assert result["start_ms"] == 500 and result["end_ms"] == 1500
        assert isinstance(result["start_ms"], int) and isinstance(result["end_ms"], int)

    def test_missing_field(self):
        data = _safe_result()
        del data["evidence"]
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(data)

    def test_wrong_types(self):
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(_safe_result(risk="yes"))
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(_safe_result(start_ms=1.5))
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(_safe_result(confidence="high"))

    def test_confidence_out_of_range(self):
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(_safe_result(confidence=1.5))

    def test_end_before_start(self):
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(_safe_result(risk=True, category="porn",
                                                         severity="low", start_ms=900, end_ms=100))

    def test_unknown_category_or_severity(self):
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(_safe_result(risk=True, category="nudity", severity="low"))
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(_safe_result(risk=True, category="porn", severity="fatal"))

    def test_risky_with_empty_category_rejected(self):
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(_safe_result(risk=True, category="none", severity="low"))

    def test_non_dict_rejected(self):
        with pytest.raises(contract.ResultValidationError):
            contract.validate_review_result(["not", "a", "dict"])

    def test_guided_output_roundtrip(self):
        # 模拟 vLLM 结构化输出：schema 约束下的 JSON 文本必须能通过校验
        raw = json.dumps(_safe_result(risk=True, category="violence", severity="medium",
                                      start_ms=0, end_ms=500), ensure_ascii=False)
        result = contract.validate_review_result(json.loads(raw))
        assert result["category"] == "violence"
