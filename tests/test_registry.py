"""任务记录注册表（registry）的单元测试。"""

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
