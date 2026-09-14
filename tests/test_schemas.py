"""schema 序列化与校验测试。"""

import pytest
from pydantic import ValidationError

from app.schemas.events import TimelineEvent
from app.schemas.verdict import HumanVerdict, VerdictRequest
from app.schemas.video import (
    FrameInfo,
    FramesInfo,
    SamplingInfo,
    VideoJob,
    VideoMetadata,
    VideoUploadResponse,
)


def test_video_upload_response_defaults() -> None:
    resp = VideoUploadResponse(video_id="vid-1", filename="demo.mp4", status="processing", sampling="fixed_fps")
    assert resp.metadata is None
    assert resp.frames is None
    data = resp.model_dump(mode="json")
    assert data["status"] == "processing"
    assert data["video_id"] == "vid-1"
    assert data["sampling"] == "fixed_fps"


def test_video_upload_response_full() -> None:
    resp = VideoUploadResponse(
        video_id="vid-2",
        filename="demo.mov",
        status="processed",
        sampling="scene",
        metadata=VideoMetadata(duration=12.5, width=1920, height=1080, fps=24.0),
        frames=FramesInfo(
            sampling=SamplingInfo(method="scene_change", threshold=0.4),
            count=1,
            frames=[FrameInfo(frame_id="000001", timestamp_ms=0, timestamp="00:00:00.000", path="/api/videos/vid-2/frames/000001.jpg")],
        ),
    )
    assert resp.metadata is not None and resp.metadata.codec is None
    assert resp.frames is not None and resp.frames.count == 1
    assert resp.frames.frames[0].timestamp_ms == 0
    assert resp.frames.sampling.method == "scene_change"
    assert resp.frames.sampling.threshold == 0.4


def test_sampling_info_method_validation() -> None:
    with pytest.raises(ValidationError):
        SamplingInfo(method="unknown")  # type: ignore[arg-type]


def test_sampling_info_fixed_fps_requires_fps() -> None:
    with pytest.raises(ValidationError):
        SamplingInfo(method="fixed_fps")


def test_sampling_info_fixed_fps_rejects_threshold() -> None:
    with pytest.raises(ValidationError):
        SamplingInfo(method="fixed_fps", fps=2.0, threshold=0.4)


def test_sampling_info_scene_change_requires_threshold() -> None:
    with pytest.raises(ValidationError):
        SamplingInfo(method="scene_change")


def test_sampling_info_scene_change_rejects_fps() -> None:
    with pytest.raises(ValidationError):
        SamplingInfo(method="scene_change", threshold=0.4, fps=2.0)


def test_sampling_info_valid_combinations() -> None:
    fixed = SamplingInfo(method="fixed_fps", fps=2.0)
    assert fixed.fps == 2.0 and fixed.threshold is None
    scene = SamplingInfo(method="scene_change", threshold=0.4)
    assert scene.threshold == 0.4 and scene.fps is None


def test_video_job_sampling_overrides_default_none() -> None:
    job = VideoJob(video_id="vid-1", filename="demo.mp4")
    assert job.frame_fps is None
    assert job.scene_threshold is None
    assert job.scene_max_frames is None
    data = job.model_dump(mode="json")
    assert data["frame_fps"] is None
    assert data["scene_threshold"] is None
    assert data["scene_max_frames"] is None


def test_video_job_sampling_overrides_roundtrip() -> None:
    job = VideoJob(
        video_id="vid-2",
        filename="demo.mp4",
        sampling="scene",
        scene_threshold=0.25,
        scene_max_frames=12,
    )
    assert job.scene_threshold == pytest.approx(0.25)
    assert job.scene_max_frames == 12
    assert job.frame_fps is None


def test_video_upload_response_invalid_status() -> None:
    with pytest.raises(ValidationError):
        VideoUploadResponse(video_id="vid-3", filename="demo.mp4", status="unknown")  # type: ignore[arg-type]


def test_frame_info_rejects_negative_timestamp() -> None:
    with pytest.raises(ValidationError):
        FrameInfo(frame_id="f", timestamp_ms=-1, timestamp="00:00:00.000", path="/x.jpg")


def test_timeline_event_valid() -> None:
    event = TimelineEvent(
        id="evt-1",
        video_id="vid-1",
        modality="ocr",
        start_ms=1000,
        end_ms=2000,
        content="示例文本",
        confidence=0.95,
        metadata={"box": [0, 0, 10, 10]},
    )
    data = event.model_dump(mode="json")
    assert data["modality"] == "ocr"
    assert data["confidence"] == 0.95
    assert data["metadata"]["box"] == [0, 0, 10, 10]


def test_timeline_event_default_metadata() -> None:
    event = TimelineEvent(
        id="evt-2",
        video_id="vid-1",
        modality="asr",
        start_ms=0,
        end_ms=500,
        content="语音内容",
        confidence=0.5,
    )
    assert event.metadata == {}


def test_timeline_event_invalid_modality() -> None:
    with pytest.raises(ValidationError):
        TimelineEvent(
            id="evt-3",
            video_id="vid-1",
            modality="text",  # type: ignore[arg-type]
            start_ms=0,
            end_ms=1,
            content="x",
            confidence=0.5,
        )


def test_verdict_request_accepts_known_decisions() -> None:
    for decision in ("approve", "reject", "false_positive"):
        payload = VerdictRequest(decision=decision, note="", event_id=None)  # type: ignore[arg-type]
        assert payload.decision == decision
        assert payload.note == ""
        assert payload.event_id is None


def test_verdict_request_rejects_unknown_decision() -> None:
    with pytest.raises(ValidationError):
        VerdictRequest(decision="maybe", note="", event_id=None)  # type: ignore[arg-type]


def test_human_verdict_created_at_json_is_iso8601() -> None:
    from datetime import datetime, timezone

    verdict = HumanVerdict(
        video_id="12345678-1234-1234-1234-1234567890ab",
        decision="approve",
        note="",
        event_id=None,
        created_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc),
    )
    data = verdict.model_dump(mode="json")
    parsed = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
    assert parsed == datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def test_timeline_event_confidence_out_of_range() -> None:
    with pytest.raises(ValidationError):
        TimelineEvent(
            id="evt-4",
            video_id="vid-1",
            modality="vlm",
            start_ms=0,
            end_ms=1,
            content="x",
            confidence=1.5,
        )
