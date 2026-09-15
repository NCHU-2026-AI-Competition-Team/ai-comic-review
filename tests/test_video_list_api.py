"""历史任务列表 GET /api/videos 的 API 级测试。

覆盖正常列表、空目录、损坏 job.json 跳过、verdict/modality 字段正确性。
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.schemas.events import TimelineEvent
from app.schemas.verdict import VerdictRequest
from app.schemas.video import VideoJob, VideoMetadata
from app.services import registry
from app.services.modality_store import write_modality_events
from app.services.verdict_store import save_verdict

OLDER_ID = "11111111-1111-1111-1111-111111111111"
NEWER_ID = "22222222-2222-2222-2222-222222222222"
CORRUPT_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """每个用例使用独立的临时存储目录，并在结束后还原配置缓存。"""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _event(video_id: str, modality: str, index: int) -> TimelineEvent:
    return TimelineEvent(
        id=f"{modality}-{index:06d}",
        video_id=video_id,
        modality=modality,  # type: ignore[arg-type]
        start_ms=index * 100,
        end_ms=index * 100 + 50,
        content=f"{modality} 事件 {index}",
        confidence=0.9,
    )


def test_list_videos_empty_directory(client: TestClient) -> None:
    """uploads 为空或不存在时返回空数组。"""
    response = client.get("/api/videos")
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_list_videos_sorted_by_created_at_desc(client: TestClient) -> None:
    """多条任务按 created_at 倒序返回摘要字段。"""
    older = VideoJob(
        video_id=OLDER_ID,
        filename="old.mp4",
        status="processed",
        created_at=datetime(2026, 1, 1, 8, 0, 0),
        metadata=VideoMetadata(duration=12.5, width=320, height=240, fps=10.0),
    )
    newer = VideoJob(
        video_id=NEWER_ID,
        filename="new.mp4",
        status="processing",
        created_at=datetime(2026, 6, 1, 12, 0, 0),
    )
    registry.save_job(older)
    registry.save_job(newer)

    response = client.get("/api/videos")
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["video_id"] for item in items] == [NEWER_ID, OLDER_ID]
    assert items[0]["filename"] == "new.mp4"
    assert items[0]["status"] == "processing"
    assert items[0]["metadata"] is None
    assert items[1]["filename"] == "old.mp4"
    assert items[1]["status"] == "processed"
    assert items[1]["metadata"]["duration"] == pytest.approx(12.5)
    assert items[1]["metadata"]["width"] == 320
    assert "frames" not in items[0]
    assert "sampling" not in items[0]


def test_list_videos_skips_corrupted_job_json(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """单个损坏 job.json 不得拖垮列表，且记 error 日志。"""
    registry.save_job(
        VideoJob(
            video_id=NEWER_ID,
            filename="ok.mp4",
            created_at=datetime(2026, 6, 1, 12, 0, 0),
        )
    )
    bad_dir = get_settings().uploads_path / CORRUPT_ID
    bad_dir.mkdir(parents=True)
    (bad_dir / "job.json").write_text("{ 不是合法 JSON", encoding="utf-8")

    schema_bad_id = "44444444-4444-4444-4444-444444444444"
    schema_dir = get_settings().uploads_path / schema_bad_id
    schema_dir.mkdir(parents=True)
    (schema_dir / "job.json").write_text('{"video_id": 123}', encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        response = client.get("/api/videos")

    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["video_id"] for item in items] == [NEWER_ID]
    assert "任务记录损坏" in caplog.text
    assert CORRUPT_ID in caplog.text
    assert schema_bad_id in caplog.text


def test_list_videos_modalities_and_verdict_fields(client: TestClient) -> None:
    """modalities 依据 outputs JSON 统计；verdict 仅在 verdict.json 存在时返回摘要。"""
    registry.save_job(
        VideoJob(
            video_id=NEWER_ID,
            filename="reviewed.mp4",
            status="processed",
            created_at=datetime(2026, 6, 1, 12, 0, 0),
        )
    )
    registry.save_job(
        VideoJob(
            video_id=OLDER_ID,
            filename="plain.mp4",
            status="processed",
            created_at=datetime(2026, 1, 1, 8, 0, 0),
        )
    )
    write_modality_events(
        NEWER_ID,
        "ocr",
        [_event(NEWER_ID, "ocr", 0), _event(NEWER_ID, "ocr", 1)],
    )
    write_modality_events(NEWER_ID, "asr", [_event(NEWER_ID, "asr", 0)])
    saved = save_verdict(
        NEWER_ID,
        VerdictRequest(decision="approve", note="通过", event_id=None),
    )

    response = client.get("/api/videos")
    assert response.status_code == 200, response.text
    items = {item["video_id"]: item for item in response.json()}

    reviewed = items[NEWER_ID]
    assert reviewed["modalities"] == {
        "ocr": {"ran": True, "event_count": 2},
        "asr": {"ran": True, "event_count": 1},
        "vlm": {"ran": False, "event_count": 0},
    }
    assert reviewed["verdict"] is not None
    assert reviewed["verdict"]["decision"] == "approve"
    assert reviewed["verdict"]["note"] == "通过"
    assert set(reviewed["verdict"].keys()) == {"decision", "note", "created_at"}
    parsed = datetime.fromisoformat(reviewed["verdict"]["created_at"].replace("Z", "+00:00"))
    assert parsed == saved.created_at.astimezone(timezone.utc)

    plain = items[OLDER_ID]
    assert plain["modalities"] == {
        "ocr": {"ran": False, "event_count": 0},
        "asr": {"ran": False, "event_count": 0},
        "vlm": {"ran": False, "event_count": 0},
    }
    assert plain["verdict"] is None
