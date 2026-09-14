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
    AUDIO_FILENAME,
    AudioExtractTimeoutError,
    InvalidAudioError,
    NoAudioTrackError,
    UploadNotFoundError,
    ensure_audio,
    extract_audio,
    is_valid_extracted_audio,
    validate_extracted_audio,
)
from app.services.uploads import find_uploaded_file

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


def _write_silence_wav(path: Path, duration_s: float = 0.1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    nframes = int(16000 * duration_s)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * nframes)


def test_extract_audio_timeout_cleans_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg 超时删除临时文件与半成品，并抛领域超时异常。"""
    uploads = get_settings().uploads_path
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / f"{VIDEO_ID}.mp4").write_bytes(b"fake-video")
    monkeypatch.setattr(audio, "_has_audio_stream", lambda path: True)
    monkeypatch.setattr(audio, "_require_tool", lambda name: "ffmpeg")

    def fake_run(cmd: list, **kwargs: object) -> None:
        dest = Path(str(cmd[-1]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"partial-wav")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    with pytest.raises(AudioExtractTimeoutError, match="超时"):
        extract_audio(VIDEO_ID)

    dest = get_settings().audio_path / VIDEO_ID / AUDIO_FILENAME
    assert not dest.exists()
    assert not dest.with_name(AUDIO_FILENAME + ".tmp").exists()


def test_extract_audio_missing_tool_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg/ffprobe 缺失时抛带指引的 RuntimeError。"""
    uploads = get_settings().uploads_path
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / f"{VIDEO_ID}.mp4").write_bytes(b"fake-video")

    def missing_tool(name: str) -> str:
        raise RuntimeError(f"未找到 {name}，请先安装 FFmpeg 并确保 {name} 在 PATH 中")

    monkeypatch.setattr(audio, "_require_tool", missing_tool)
    with pytest.raises(RuntimeError, match="未找到"):
        extract_audio(VIDEO_ID)
    dest = get_settings().audio_path / VIDEO_ID / AUDIO_FILENAME
    assert not dest.exists()


def test_ensure_audio_rejects_empty_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """空 audio.wav 不得复用，应删除后重新提取。"""
    dest = get_settings().audio_path / VIDEO_ID / AUDIO_FILENAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"")
    extracted = dest.with_name("extracted.wav")
    called: list[str] = []

    def fake_extract(video_id: str) -> Path:
        called.append(video_id)
        return extracted

    monkeypatch.setattr(audio, "extract_audio", fake_extract)
    result = ensure_audio(VIDEO_ID)
    assert called == [VIDEO_ID]
    assert result == extracted
    assert not dest.exists()


def test_ensure_audio_rejects_corrupt_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """损坏的 RIFF 头不得复用。"""
    dest = get_settings().audio_path / VIDEO_ID / AUDIO_FILENAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"RIFF" + b"\x00" * 40)
    extracted = dest.with_name("extracted.wav")
    monkeypatch.setattr(audio, "extract_audio", lambda video_id: extracted)
    result = ensure_audio(VIDEO_ID)
    assert result == extracted
    assert not dest.exists()


def test_validate_extracted_audio_accepts_16k_mono(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """wave 校验通过后，ffprobe 探测由桩代替。"""
    monkeypatch.setattr(audio, "_probe_audio_recognizable", lambda path: None)
    path = tmp_path / "ok.wav"
    _write_silence_wav(path)
    validate_extracted_audio(path)
    assert is_valid_extracted_audio(path) is True


def test_validate_extracted_audio_rejects_wrong_format(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """wave 可打开但不是 16kHz 单声道时拒绝。"""
    monkeypatch.setattr(audio, "_probe_audio_recognizable", lambda path: None)
    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\x00\x00" * 8)
    with pytest.raises(InvalidAudioError, match="格式不符"):
        validate_extracted_audio(stereo)
    assert is_valid_extracted_audio(stereo) is False


def test_find_uploaded_file_enumerates_allowed_extensions_only() -> None:
    """同名 .txt 不被 glob 命中，只有允许的视频扩展名才算上传文件。"""
    uploads = get_settings().uploads_path
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / f"{VIDEO_ID}.txt").write_bytes(b"not-video")
    with pytest.raises(UploadNotFoundError):
        find_uploaded_file(VIDEO_ID)
    (uploads / f"{VIDEO_ID}.mov").write_bytes(b"ok")
    found = find_uploaded_file(VIDEO_ID)
    assert found.suffix == ".mov"
