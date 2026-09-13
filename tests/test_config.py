"""配置项加载与校验测试。"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_frame_extraction_fps_zero_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRAME_EXTRACTION_FPS=0 应在配置加载时明确报错。"""
    monkeypatch.setenv("FRAME_EXTRACTION_FPS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_frame_extraction_fps_negative_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAME_EXTRACTION_FPS", "-1.5")
    with pytest.raises(ValidationError):
        Settings()


def test_frame_extraction_fps_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAME_EXTRACTION_FPS", "1.5")
    assert Settings().frame_extraction_fps == 1.5
