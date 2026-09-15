"""任务记录注册表（registry）的单元测试。"""

from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest

from app.core.config import get_settings
from app.schemas.video import VideoJob
from app.services import registry

VALID_VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每个用例使用独立的临时存储目录，并在结束后还原配置缓存。"""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_get_job_missing_returns_none(storage: Path) -> None:
    assert registry.get_job(VALID_VIDEO_ID) is None


def test_get_job_corrupted_raises(storage: Path) -> None:
    """job.json 存在但损坏时抛出 JobCorruptedError，而非静默返回 None。"""
    job_dir = storage / "uploads" / VALID_VIDEO_ID
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text("{ 不是合法 JSON", encoding="utf-8")

    with pytest.raises(registry.JobCorruptedError):
        registry.get_job(VALID_VIDEO_ID)


def test_get_job_invalid_schema_raises(storage: Path) -> None:
    """JSON 合法但字段不符合 VideoJob 模型时同样视为损坏。"""
    job_dir = storage / "uploads" / VALID_VIDEO_ID
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text('{"video_id": 123}', encoding="utf-8")

    with pytest.raises(registry.JobCorruptedError):
        registry.get_job(VALID_VIDEO_ID)


def test_save_and_get_job_roundtrip(storage: Path) -> None:
    saved = registry.save_job(VideoJob(video_id=VALID_VIDEO_ID, filename="demo.mp4"))
    loaded = registry.get_job(VALID_VIDEO_ID)
    assert loaded is not None
    assert loaded.video_id == saved.video_id
    assert loaded.status == "processing"


def test_list_jobs_empty_when_uploads_missing(storage: Path) -> None:
    assert registry.list_jobs() == []


def test_list_jobs_empty_directory(storage: Path) -> None:
    (storage / "uploads").mkdir(parents=True)
    assert registry.list_jobs() == []


def test_list_jobs_skips_corrupted_and_sorts_desc(storage: Path) -> None:
    older_id = "11111111-1111-1111-1111-111111111111"
    newer_id = "22222222-2222-2222-2222-222222222222"
    registry.save_job(
        VideoJob(
            video_id=older_id,
            filename="old.mp4",
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        )
    )
    registry.save_job(
        VideoJob(
            video_id=newer_id,
            filename="new.mp4",
            created_at=datetime(2026, 6, 1, 0, 0, 0),
        )
    )
    bad_dir = storage / "uploads" / "33333333-3333-3333-3333-333333333333"
    bad_dir.mkdir(parents=True)
    (bad_dir / "job.json").write_text("{ 不是合法 JSON", encoding="utf-8")

    jobs = registry.list_jobs()
    assert [job.video_id for job in jobs] == [newer_id, older_id]

