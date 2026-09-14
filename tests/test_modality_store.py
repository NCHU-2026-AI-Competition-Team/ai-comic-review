"""模态事件落盘公共工具测试。"""

import json
from pathlib import Path
from typing import Iterator

import pytest

from app.core.config import get_settings
from app.schemas.events import TimelineEvent
from app.services.modality_store import (
    modality_result_path,
    try_load_modality_events,
    write_modality_events,
)

VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _event() -> TimelineEvent:
    return TimelineEvent(
        id="asr-000000",
        video_id=VIDEO_ID,
        modality="asr",
        start_ms=0,
        end_ms=100,
        content="台词",
        confidence=0.9,
        metadata={"language": "zh", "segment_index": 0},
    )


def test_write_and_load_roundtrip() -> None:
    path = write_modality_events(VIDEO_ID, "asr", [_event()])
    assert path == modality_result_path(VIDEO_ID, "asr")
    assert not path.with_name("asr.json.tmp").exists()
    loaded = try_load_modality_events(VIDEO_ID, "asr")
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0].content == "台词"


def test_try_load_missing_returns_none() -> None:
    assert try_load_modality_events(VIDEO_ID, "asr") is None


def test_try_load_corrupted_deletes_and_returns_none() -> None:
    path = modality_result_path(VIDEO_ID, "ocr")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 损坏", encoding="utf-8")
    assert try_load_modality_events(VIDEO_ID, "ocr") is None
    assert not path.exists()


def test_try_load_mismatch_deletes_and_returns_none() -> None:
    path = write_modality_events(VIDEO_ID, "asr", [_event()])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["video_id"] = "00000000-0000-0000-0000-000000000000"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert try_load_modality_events(VIDEO_ID, "asr") is None
    assert not path.exists()
