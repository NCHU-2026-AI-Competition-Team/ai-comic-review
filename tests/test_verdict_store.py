"""人工复核结论落盘工具测试。"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from app.core.config import get_settings
from app.schemas.verdict import VerdictRequest
from app.services.verdict_store import (
    save_verdict,
    try_load_stored_verdict,
    try_load_verdict,
    verdict_path,
)

VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_write_and_load_roundtrip() -> None:
    request = VerdictRequest(decision="approve", note="ok", event_id=None)
    saved = save_verdict(VIDEO_ID, request)
    path = verdict_path(VIDEO_ID)
    assert path.is_file()
    assert not path.with_name("verdict.json.tmp").exists()
    loaded = try_load_verdict(VIDEO_ID)
    assert loaded is not None
    assert loaded.decision == "approve"
    assert loaded.note == "ok"
    assert loaded.event_id is None
    assert loaded.created_at == saved.created_at
    stored = try_load_stored_verdict(VIDEO_ID)
    assert stored is not None
    assert stored.updated_at == stored.created_at


def test_try_load_missing_returns_none() -> None:
    assert try_load_verdict(VIDEO_ID) is None
    assert try_load_stored_verdict(VIDEO_ID) is None


def test_try_load_corrupted_deletes_and_returns_none() -> None:
    path = verdict_path(VIDEO_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 损坏", encoding="utf-8")
    assert try_load_verdict(VIDEO_ID) is None
    assert not path.exists()


def test_overwrite_keeps_created_at_and_refreshes_updated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = [
        datetime(2026, 9, 14, 8, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 9, 0, 0, tzinfo=timezone.utc),
    ]
    clock = iter(times)
    monkeypatch.setattr("app.services.verdict_store._now", lambda: next(clock))

    save_verdict(VIDEO_ID, VerdictRequest(decision="approve", note="", event_id=None))
    save_verdict(
        VIDEO_ID,
        VerdictRequest(decision="false_positive", note="误报", event_id="evt-1"),
    )
    stored = try_load_stored_verdict(VIDEO_ID)
    assert stored is not None
    assert stored.decision == "false_positive"
    assert stored.note == "误报"
    assert stored.event_id == "evt-1"
    assert stored.created_at == times[0]
    assert stored.updated_at == times[1]
