"""VLM 审核流水线的纯逻辑单元测试。

不依赖真实 Modal 服务：通过注入 fake Provider 隔离引擎，
覆盖时间窗展开、幂等、32B 501 降级、JSON 不合格占位与风险融合接线。
"""

import ast
import json
from pathlib import Path

import pytest

from ai.vlm.base import VlmEscalationUnavailableError, VlmResponseFormatError, VlmReviewInput
from ai.vlm.schemas import VlmReviewResult
from app.core.config import get_settings
from app.schemas.video import FrameInfo
from app.services import vlm_pipeline
from app.services.modality_store import write_modality_events
from app.services.vlm_pipeline import (
    FramesNotFoundError,
    expand_review_to_events,
    join_modality_text,
    run_review,
)
from app.schemas.events import TimelineEvent

VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("MODAL_VLM_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("VLM_PRIMARY", "qwen3-vl-8b-instruct")
    monkeypatch.setenv("VLM_ESCALATION", "qwen3-vl-32b-instruct")
    monkeypatch.setenv("VLM_LOW_CONFIDENCE_THRESHOLD", "0.6")
    monkeypatch.setenv("VLM_MAX_FRAMES_PER_REQUEST", "8")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _result(**overrides) -> VlmReviewResult:
    data = {
        "risk": True,
        "category": "violence",
        "severity": "medium",
        "confidence": 0.9,
        "start_ms": 500,
        "end_ms": 1000,
        "evidence": "第2帧含暴力",
        "reason": "画面冲突",
        "suggestion": "复核",
    }
    data.update(overrides)
    if not data.get("risk"):
        data["category"] = "none"
        data["severity"] = "none"
        data["start_ms"] = 0
        data["end_ms"] = 0
    return VlmReviewResult.model_validate(data)


def _frame(frame_id: str, timestamp_ms: int) -> FrameInfo:
    return FrameInfo(
        frame_id=frame_id,
        timestamp_ms=timestamp_ms,
        timestamp="00:00:00.000",
        path=f"/api/videos/{VIDEO_ID}/frames/{frame_id}.jpg",
    )


def _write_frames_json(frames: list[FrameInfo]) -> None:
    frames_dir = get_settings().frames_path / VIDEO_ID
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "frames.json").write_text(
        json.dumps(
            {
                "sampling": {"method": "fixed_fps", "fps": 2.0, "threshold": None},
                "count": len(frames),
                "frames": [frame.model_dump(mode="json") for frame in frames],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for frame in frames:
        (frames_dir / f"{frame.frame_id}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 8)


class FakeProvider:
    def __init__(
        self,
        result: VlmReviewResult | None = None,
        *,
        by_model: dict[str, VlmReviewResult] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[VlmReviewInput] = []
        self._result = result or _result()
        self._by_model = by_model or {}
        self._errors = errors or {}

    def review(self, payload: VlmReviewInput) -> VlmReviewResult:
        self.calls.append(payload)
        model = payload.model or "qwen3-vl-8b-instruct"
        if model in self._errors:
            raise self._errors[model]
        if model in self._by_model:
            return self._by_model[model]
        if "32b" in model:
            raise VlmEscalationUnavailableError("501")
        return self._result


def test_expand_risk_window_to_overlapping_frames() -> None:
    batch = [_frame("frame_000000", 0), _frame("frame_000001", 500), _frame("frame_000002", 1000)]
    events = expand_review_to_events(
        VIDEO_ID,
        0,
        batch,
        _result(start_ms=500, end_ms=1000),
        model="qwen3-vl-8b-instruct",
        escalation_status="not_needed",
        escalation_reasons=(),
    )
    assert len(events) == 2
    assert [event.start_ms for event in events] == [500, 1000]
    assert events[0].modality == "vlm"
    assert events[0].metadata["risk"] is True
    assert events[0].metadata["category"] == "violence"
    assert events[0].metadata["frame_ids"] == ["frame_000001"]
    assert events[0].content == "第2帧含暴力"


def test_expand_risk_window_without_matching_frame_keeps_window() -> None:
    batch = [_frame("frame_000000", 0)]
    events = expand_review_to_events(
        VIDEO_ID,
        0,
        batch,
        _result(start_ms=800, end_ms=1200),
        model="qwen3-vl-8b-instruct",
        escalation_status="not_needed",
        escalation_reasons=(),
    )
    assert len(events) == 1
    assert events[0].start_ms == 800 and events[0].end_ms == 1200
    assert events[0].id == "vlm-0000"


def test_expand_safe_result_covers_batch_window() -> None:
    batch = [_frame("frame_000000", 0), _frame("frame_000001", 500)]
    events = expand_review_to_events(
        VIDEO_ID,
        1,
        batch,
        _result(risk=False, category="none", severity="none", start_ms=0, end_ms=0),
        model="qwen3-vl-8b-instruct",
        escalation_status="not_needed",
        escalation_reasons=(),
    )
    assert len(events) == 1
    assert events[0].start_ms == 0 and events[0].end_ms == 500
    assert events[0].metadata["risk"] is False
    assert events[0].id == "vlm-0001"


def test_join_modality_text_filters_by_window() -> None:
    events = [
        TimelineEvent(
            id="ocr-a",
            video_id=VIDEO_ID,
            modality="ocr",
            start_ms=0,
            end_ms=0,
            content="前段",
            confidence=0.9,
        ),
        TimelineEvent(
            id="ocr-b",
            video_id=VIDEO_ID,
            modality="ocr",
            start_ms=800,
            end_ms=800,
            content="后段",
            confidence=0.9,
        ),
    ]
    text = join_modality_text(events, 0, 500)
    assert "前段" in text
    assert "后段" not in text


def test_run_review_writes_result_file() -> None:
    _write_frames_json(
        [_frame("frame_000000", 0), _frame("frame_000001", 500), _frame("frame_000002", 1000)]
    )
    outcome = run_review(VIDEO_ID, provider=FakeProvider())
    assert outcome.reused is False
    assert len(outcome.events) == 2
    payload = json.loads(
        (get_settings().outputs_path / VIDEO_ID / "vlm.json").read_text(encoding="utf-8")
    )
    assert payload["modality"] == "vlm"
    assert payload["video_id"] == VIDEO_ID
    assert len(payload["events"]) == 2


def test_run_review_reuses_existing_result() -> None:
    _write_frames_json([_frame("frame_000000", 0), _frame("frame_000001", 500)])
    first = run_review(VIDEO_ID, provider=FakeProvider())
    assert first.reused is False

    class ExplodingProvider:
        def review(self, payload: VlmReviewInput) -> VlmReviewResult:
            raise AssertionError("复用已有结果时不应再次审核")

    second = run_review(VIDEO_ID, provider=ExplodingProvider())
    assert second.reused is True
    assert len(second.events) == len(first.events)


def test_run_review_force_reruns() -> None:
    _write_frames_json([_frame("frame_000000", 0), _frame("frame_000001", 500)])
    run_review(VIDEO_ID, provider=FakeProvider())
    replacement = _result(
        category="blood",
        severity="high",
        evidence="新证据",
        start_ms=0,
        end_ms=500,
    )
    outcome = run_review(VIDEO_ID, provider=FakeProvider(replacement), force=True)
    assert outcome.reused is False
    assert outcome.events[0].metadata["category"] == "blood"


def test_run_review_missing_frames_raises() -> None:
    with pytest.raises(FramesNotFoundError):
        run_review(VIDEO_ID, provider=FakeProvider())


def test_run_review_high_risk_32b_501_degrades() -> None:
    """高风险触发 escalation；32B 返回 501 时保留 8B 并标注待复审。"""
    _write_frames_json([_frame("frame_000000", 0), _frame("frame_000001", 500)])
    primary = _result(severity="high", confidence=0.99, start_ms=0, end_ms=500)
    provider = FakeProvider(
        primary,
        errors={"qwen3-vl-32b-instruct": VlmEscalationUnavailableError("501")},
    )
    outcome = run_review(VIDEO_ID, provider=provider)
    assert outcome.needs_escalation is True
    assert outcome.escalation_status == "pending_review"
    assert any(event.metadata.get("escalation_status") == "pending_review" for event in outcome.events)
    models = [payload.model for payload in provider.calls]
    assert "qwen3-vl-8b-instruct" in models
    assert "qwen3-vl-32b-instruct" in models


def test_run_review_json_invalid_32b_501_placeholder() -> None:
    _write_frames_json([_frame("frame_000000", 0)])
    provider = FakeProvider(
        errors={
            "qwen3-vl-8b-instruct": VlmResponseFormatError("结构非法"),
            "qwen3-vl-32b-instruct": VlmEscalationUnavailableError("501"),
        }
    )
    outcome = run_review(VIDEO_ID, provider=provider)
    assert outcome.needs_escalation is True
    assert outcome.events[0].metadata["suggestion"] == "待复审"
    assert outcome.events[0].metadata["risk"] is True
    assert "不合规" in outcome.events[0].metadata["reason"]


def test_run_review_batches_by_max_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLM_MAX_FRAMES_PER_REQUEST", "2")
    get_settings.cache_clear()
    frames = [_frame(f"frame_{index:06d}", index * 500) for index in range(5)]
    _write_frames_json(frames)
    provider = FakeProvider(_result(risk=False, category="none", severity="none"))
    run_review(VIDEO_ID, provider=provider)
    assert len(provider.calls) == 3
    assert [len(call.frame_paths) for call in provider.calls] == [2, 2, 1]


def test_run_review_passes_ocr_asr_context() -> None:
    _write_frames_json([_frame("frame_000000", 0), _frame("frame_000001", 500)])
    write_modality_events(
        VIDEO_ID,
        "ocr",
        [
            TimelineEvent(
                id="ocr-1",
                video_id=VIDEO_ID,
                modality="ocr",
                start_ms=0,
                end_ms=0,
                content="他拿起刀刺向对方",
                confidence=0.9,
            )
        ],
    )
    write_modality_events(
        VIDEO_ID,
        "asr",
        [
            TimelineEvent(
                id="asr-1",
                video_id=VIDEO_ID,
                modality="asr",
                start_ms=0,
                end_ms=400,
                content="你给我去死吧",
                confidence=0.8,
            )
        ],
    )
    provider = FakeProvider(_result(risk=False, category="none", severity="none"))
    outcome = run_review(VIDEO_ID, provider=provider)
    assert "刺向" in provider.calls[0].ocr_text
    assert "去死" in provider.calls[0].asr_text
    # 模态冲突：VLM 报无风险但 OCR/ASR 有暴力信号 → 待复审
    assert outcome.needs_escalation is True


def test_run_review_atomic_write_leaves_no_tmp() -> None:
    _write_frames_json([_frame("frame_000000", 0)])
    run_review(VIDEO_ID, provider=FakeProvider(_result(risk=False, category="none", severity="none")))
    result_dir = get_settings().outputs_path / VIDEO_ID
    assert (result_dir / "vlm.json").is_file()
    assert not (result_dir / "vlm.json.tmp").exists()


def test_run_review_default_provider_uses_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_frames_json([_frame("frame_000000", 0)])
    called: list[bool] = []

    def fake_get_default_provider():
        called.append(True)
        return FakeProvider(_result(risk=False, category="none", severity="none"))

    monkeypatch.setattr(vlm_pipeline, "get_default_provider", fake_get_default_provider)
    run_review(VIDEO_ID)
    assert called == [True]


def test_pipeline_does_not_import_concrete_provider() -> None:
    source = Path(vlm_pipeline.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "ai.vlm.qwen" not in imported_modules
    assert not any(
        module.startswith("ai.vlm.")
        and module not in {"ai.vlm.base", "ai.vlm.factory", "ai.vlm.escalate", "ai.vlm.schemas"}
        for module in imported_modules
    ), f"vlm_pipeline 引入了抽象/工厂/判定以外的 ai.vlm 模块：{imported_modules}"


def test_run_review_corrupted_result_reruns() -> None:
    _write_frames_json([_frame("frame_000000", 0)])
    result_file = get_settings().outputs_path / VIDEO_ID / "vlm.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text("{ 不是合法 JSON", encoding="utf-8")
    outcome = run_review(
        VIDEO_ID, provider=FakeProvider(_result(risk=False, category="none", severity="none"))
    )
    assert outcome.reused is False
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["modality"] == "vlm"
