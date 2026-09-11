"""schema 序列化与校验测试。"""

import pytest
from pydantic import ValidationError

from app.schemas.events import TimelineEvent
from app.schemas.video import (
    FrameInfo,
    FramesInfo,
    VideoMetadata,
    VideoUploadResponse,
)


def test_video_upload_response_defaults() -> None:
    resp = VideoUploadResponse(video_id="vid-1", filename="demo.mp4", status="processing")
    assert resp.metadata is None
    assert resp.frames is None
    data = resp.model_dump(mode="json")
    assert data["status"] == "processing"
    assert data["video_id"] == "vid-1"


def test_video_upload_response_full() -> None:
    resp = VideoUploadResponse(
        video_id="vid-2",
        filename="demo.mov",
        status="processed",
        metadata=VideoMetadata(duration=12.5, width=1920, height=1080, fps=24.0),
        frames=FramesInfo(
            count=1,
            frames=[FrameInfo(frame_id="000001", timestamp_ms=0, timestamp="00:00:00.000", path="/api/videos/vid-2/frames/000001.jpg")],
        ),
    )
    assert resp.metadata is not None and resp.metadata.codec is None
    assert resp.frames is not None and resp.frames.count == 1
    assert resp.frames.frames[0].timestamp_ms == 0


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
