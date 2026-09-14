"""音频提取服务的测试。

真实 ffmpeg/ffprobe 用例在本机未安装 FFmpeg 时自动跳过；
含音轨/无音轨的测试视频由 ffmpeg lavfi 现场生成。
"""

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services import audio
from app.services.audio import (
    NoAudioTrackError,
    UploadNotFoundError,
    ensure_audio,
    extract_audio,
)

VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="未安装 ffmpeg/ffprobe")


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例使用独立的临时存储目录，并在结束后还原配置缓存。"""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", *args], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"测试视频生成失败: {result.stderr[-500:]}"


def _make_video_with_audio(dest: Path) -> None:
    """生成 1 秒含 440Hz 正弦音轨的测试视频。"""
    _run_ffmpeg(
        [
            "-y",
            "-f", "lavfi", "-i", "color=size=64x64:duration=1:rate=5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-shortest",
            str(dest),
        ]
    )


def _make_video_without_audio(dest: Path) -> None:
    """生成 1 秒无音轨的测试视频。"""
    _run_ffmpeg(
        ["-y", "-f", "lavfi", "-i", "color=size=64x64:duration=1:rate=5", str(dest)]
    )


def test_extract_audio_missing_upload_raises() -> None:
    """上传文件不存在时抛 UploadNotFoundError（无需 ffmpeg 即可触发）。"""
    with pytest.raises(UploadNotFoundError):
        extract_audio(VIDEO_ID)


@requires_ffmpeg
def test_extract_audio_produces_16khz_mono_wav() -> None:
    """提取产物为 16kHz 单声道 16bit PCM wav，且时长与源视频接近。"""
    video_path = get_settings().uploads_path / f"{VIDEO_ID}.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    _make_video_with_audio(video_path)

    dest = extract_audio(VIDEO_ID)

    assert dest == get_settings().audio_path / VIDEO_ID / "audio.wav"
    assert dest.is_file()
    with wave.open(str(dest), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        duration = wav.getnframes() / wav.getframerate()
        assert 0.5 < duration < 2.0


@requires_ffmpeg
def test_extract_audio_no_audio_track_raises() -> None:
    """无音轨视频抛 NoAudioTrackError，且不残留产物文件。"""
    video_path = get_settings().uploads_path / f"{VIDEO_ID}.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    _make_video_without_audio(video_path)

    with pytest.raises(NoAudioTrackError, match="不含音轨"):
        extract_audio(VIDEO_ID)
    assert not (get_settings().audio_path / VIDEO_ID / "audio.wav").exists()


@requires_ffmpeg
def test_ensure_audio_reuses_existing_file() -> None:
    """audio.wav 已存在时 ensure_audio 直接复用，不重复调用 ffmpeg。"""
    video_path = get_settings().uploads_path / f"{VIDEO_ID}.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    _make_video_with_audio(video_path)
    first = extract_audio(VIDEO_ID)
    mtime = first.stat().st_mtime_ns

    second = ensure_audio(VIDEO_ID)

    assert second == first
    assert second.stat().st_mtime_ns == mtime
