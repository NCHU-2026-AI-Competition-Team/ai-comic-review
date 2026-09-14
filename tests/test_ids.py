"""video_id 规范化与路径安全测试。"""

from pathlib import Path

import pytest

from app.core.ids import InvalidVideoIdError, normalize_video_id
from app.services.storage_paths import UnsafePathError, resolve_in_dir


def test_normalize_video_id_lowercases_uuid() -> None:
    raw = "12345678-1234-1234-1234-1234567890AB"
    assert normalize_video_id(raw) == "12345678-1234-1234-1234-1234567890ab"


def test_normalize_video_id_rejects_path_traversal() -> None:
    with pytest.raises(InvalidVideoIdError):
        normalize_video_id("../etc/passwd")
    with pytest.raises(InvalidVideoIdError):
        normalize_video_id("not-a-uuid")


def test_resolve_in_dir_rejects_escape(tmp_path: Path) -> None:
    base = tmp_path / "audio"
    base.mkdir()
    with pytest.raises(UnsafePathError):
        resolve_in_dir(base, "..", "etc", "passwd")
    inside = resolve_in_dir(base, "12345678-1234-1234-1234-1234567890ab", "audio.wav")
    assert inside.is_relative_to(base.resolve())
