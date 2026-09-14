"""escalation 判定与风险融合的纯逻辑测试。"""

import pytest

from ai.vlm.escalate import (
    detect_text_categories,
    fuse_overall,
    has_modality_conflict,
    should_escalate,
)
from ai.vlm.schemas import VlmReviewResult


def _result(**overrides) -> VlmReviewResult:
    data = {
        "risk": True,
        "category": "violence",
        "severity": "medium",
        "confidence": 0.9,
        "start_ms": 0,
        "end_ms": 500,
        "evidence": "画面含冲突",
        "reason": "暴力倾向",
        "suggestion": "复核",
    }
    data.update(overrides)
    if not data.get("risk"):
        data["category"] = "none"
        data["severity"] = "none"
        data["start_ms"] = 0
        data["end_ms"] = 0
    return VlmReviewResult.model_validate(data)


def test_should_escalate_low_confidence() -> None:
    decision = should_escalate(_result(confidence=0.4), low_confidence_threshold=0.6)
    assert decision.needs_escalation is True
    assert "low_confidence" in decision.reasons


def test_should_escalate_high_risk() -> None:
    decision = should_escalate(_result(severity="high", confidence=0.99))
    assert decision.needs_escalation is True
    assert "high_risk" in decision.reasons


def test_should_escalate_json_invalid() -> None:
    decision = should_escalate(None, json_invalid=True)
    assert decision.needs_escalation is True
    assert decision.reasons == ("json_invalid",)


def test_should_escalate_modality_conflict() -> None:
    safe = _result(risk=False, category="none", severity="none", start_ms=0, end_ms=0)
    decision = should_escalate(
        safe,
        ocr_text="他拿起刀刺向对方，鲜血直流",
        asr_text="你给我去死吧",
    )
    assert decision.needs_escalation is True
    assert "modality_conflict" in decision.reasons


def test_should_not_escalate_aligned_risk() -> None:
    decision = should_escalate(
        _result(confidence=0.9, severity="medium"),
        ocr_text="他拿起刀刺向对方",
        asr_text="你给我去死吧",
    )
    assert decision.needs_escalation is False
    assert decision.reasons == ()


def test_detect_text_categories() -> None:
    found = detect_text_categories("他拿起刀刺向对方，鲜血直流")
    assert "violence" in found
    assert "blood" in found


def test_has_modality_conflict_only_when_vlm_safe() -> None:
    risky = _result()
    assert has_modality_conflict(risky, ocr_text="刺向") is False
    safe = _result(risk=False, category="none", severity="none")
    assert has_modality_conflict(safe, ocr_text="刺向") is True
    assert has_modality_conflict(safe, ocr_text="日常对话") is False


def test_fuse_overall_takes_highest_severity() -> None:
    results = [
        _result(severity="low", confidence=0.99, suggestion="复核"),
        _result(category="blood", severity="high", confidence=0.8, suggestion="拦截"),
    ]
    verdict = fuse_overall(results, needs_escalation=False, escalation_status="not_needed")
    assert verdict.risk is True
    assert verdict.severity == "high"
    assert verdict.category == "blood"
    assert verdict.suggestion == "拦截"


def test_fuse_overall_pending_prefixes_suggestion() -> None:
    verdict = fuse_overall(
        [_result(severity="high", suggestion="拦截")],
        needs_escalation=True,
        escalation_status="pending_review",
    )
    assert verdict.needs_escalation is True
    assert verdict.suggestion.startswith("待复审")
    assert "拦截" in verdict.suggestion


def test_fuse_overall_text_signal_without_vlm_risk() -> None:
    safe = _result(risk=False, category="none", severity="none")
    verdict = fuse_overall(
        [safe],
        needs_escalation=False,
        escalation_status="not_needed",
        ocr_text="他拿起刀刺向对方",
        asr_text="",
    )
    assert verdict.risk is True
    assert verdict.needs_escalation is True
    assert verdict.severity == "low"
    assert verdict.suggestion == "待复审"
    assert verdict.category == "violence"


def test_fuse_overall_all_safe() -> None:
    safe = _result(risk=False, category="none", severity="none", confidence=0.99)
    verdict = fuse_overall([safe], needs_escalation=False, escalation_status="not_needed")
    assert verdict.risk is False
    assert verdict.category == "none"
    assert verdict.suggestion == "通过"


def test_fuse_overall_empty_results_safe() -> None:
    verdict = fuse_overall([], needs_escalation=False, escalation_status="not_needed")
    assert verdict.risk is False
    assert verdict.suggestion == "通过"
