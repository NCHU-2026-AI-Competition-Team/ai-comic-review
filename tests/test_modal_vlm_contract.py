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


# ---------------------------------------------------------------------------
# 高分辨率帧降采样与图像 token 估算
# ---------------------------------------------------------------------------

_SERVE_PATH = Path(__file__).resolve().parents[1] / "modal" / "vlm" / "serve.py"


class TestFitLongEdge:
    def test_1080p_to_768(self):
        assert contract.fit_long_edge(1920, 1080) == (768, 432)

    def test_real_video_1760x1184(self):
        # 用户 17601184 实测源分辨率
        width, height = contract.fit_long_edge(1760, 1184)
        assert width == contract.FRAME_LONG_EDGE
        assert height == round(1184 * 768 / 1760)
        assert max(width, height) == 768
        assert abs(width / height - 1760 / 1184) < 0.01

    def test_low_res_unchanged(self):
        assert contract.fit_long_edge(640, 360) == (640, 360)

    def test_portrait_uses_height_as_long_edge(self):
        assert contract.fit_long_edge(1080, 1920) == (432, 768)

    def test_square(self):
        assert contract.fit_long_edge(2000, 2000) == (768, 768)

    def test_invalid_size_raises(self):
        with pytest.raises(contract.InputValidationError):
            contract.fit_long_edge(0, 100)

    def test_matches_pil_resize(self):
        from PIL import Image

        original = Image.new("RGB", (1920, 1080), (12, 34, 56))
        target = contract.fit_long_edge(*original.size)
        resized = original.resize(target, Image.Resampling.LANCZOS)
        assert resized.size == (768, 432)
        assert resized.mode == "RGB"


class TestImageTokenEstimate:
    def test_low_res_three_frames_fit_old_8192(self):
        sizes = [(640, 360)] * 3
        prompt = contract.build_review_prompt([0, 500, 1000], ocr_text="日常字幕", asr_text="你好")
        tokens = contract.ensure_review_fits_model_len(
            sizes, prompt, max_model_len=8192, max_output_tokens=1024
        )
        assert tokens + 1024 <= 8192
        assert contract.estimate_image_tokens(640, 360) < 400

    def test_hires_eight_frames_without_downsample_exceed_old_8192(self):
        sizes = [(1760, 1184)] * 8
        prompt = contract.build_review_prompt(list(range(0, 4000, 500)))
        image_tokens = sum(contract.estimate_image_tokens(w, h) for w, h in sizes)
        # 根因：约 2.1 万图像 token，远超旧上限
        assert image_tokens > 8192
        with pytest.raises(contract.InputValidationError, match="图像 token"):
            contract.ensure_review_fits_model_len(
                sizes, prompt, max_model_len=8192, max_output_tokens=1024
            )

    def test_hires_eight_frames_after_downsample_fit(self):
        sizes = [contract.fit_long_edge(1760, 1184)] * 8
        prompt = contract.build_review_prompt(
            list(range(0, 4000, 500)),
            ocr_text="他拿起刀刺向对方，鲜血直流",
            asr_text="你给我去死吧",
        )
        tokens = contract.ensure_review_fits_model_len(sizes, prompt)
        assert tokens + contract.MAX_OUTPUT_TOKENS <= contract.MAX_MODEL_LEN
        per_frame = contract.estimate_image_tokens(*sizes[0])
        assert per_frame < 800
        assert per_frame * 8 < 5000

    def test_over_budget_text_returns_readable_error(self):
        sizes = [contract.fit_long_edge(1920, 1080)] * 8
        huge = "字" * contract.MAX_TEXT_FIELD_CHARS
        prompt = contract.build_review_prompt(
            list(range(0, 4000, 500)), ocr_text=huge, asr_text=huge
        )
        with pytest.raises(contract.InputValidationError, match="图像 token 估算超限") as exc_info:
            contract.ensure_review_fits_model_len(sizes, prompt)
        message = str(exc_info.value)
        assert "max_model_len" in message
        assert "8 帧" in message

    def test_model_len_error_detection(self):
        assert contract.is_model_len_error(
            ValueError("The decoder prompt (length 25000) is longer than the maximum model length of 8192")
        )
        assert contract.is_model_len_error(RuntimeError("prompt exceeds max_model_len=8192"))
        assert not contract.is_model_len_error(ValueError("invalid JSON"))


def test_serve_source_downsamples_before_vllm():
    source = _SERVE_PATH.read_text(encoding="utf-8")
    assert "fit_long_edge" in source
    assert "FRAME_LONG_EDGE" in source
    assert "ensure_review_fits_model_len" in source
    assert "is_model_len_error" in source
    assert "Image.Resampling.LANCZOS" in source
    assert "max_model_len=MAX_MODEL_LEN" in source
    assert "limit_mm_per_prompt={\"image\": MAX_FRAMES_PER_REQUEST}" in source
    assert contract.MAX_FRAMES_PER_REQUEST == 8
    assert contract.FRAME_LONG_EDGE == 768
