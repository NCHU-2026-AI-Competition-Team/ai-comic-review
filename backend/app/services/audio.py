"""音频提取：从已上传视频提取 ASR 所需的单声道 16kHz PCM wav。

对照 services/video 包的模式：以 subprocess.run 参数列表形式调用
ffprobe/ffmpeg（无 shell），工具缺失、无音轨与提取失败分别给出明确错误。
产物落盘 storage/audio/{video_id}/audio.wav，供 ai.asr Provider 上传识别；
采样率与声道数由 ASR 服务契约固定（16kHz 单声道 PCM wav），不走配置。
写入走临时文件 + 校验 + 原子替换，超时或失败不残留半成品。
"""

import logging
import os
import subprocess
import wave
from pathlib import Path

from app.core.config import get_settings
from app.core.ids import normalize_video_id
from app.services.storage_paths import resolve_in_dir
from app.services.uploads import UploadNotFoundError, find_uploaded_file
from app.services.video import _require_tool

logger = logging.getLogger(__name__)

AUDIO_FILENAME = "audio.wav"
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
PROBE_TIMEOUT_SECONDS = 60
EXTRACT_TIMEOUT_SECONDS = 300


class NoAudioTrackError(RuntimeError):
    """视频不含音轨，无法执行 ASR。"""


class AudioExtractTimeoutError(RuntimeError):
    """ffprobe/ffmpeg 音频处理超时。"""


class InvalidAudioError(RuntimeError):
    """音频产物为空、损坏或不符合 16kHz 单声道 wav 契约。"""


def audio_file_path(video_id: str) -> Path:
    """音频产物路径：storage/audio/{video_id}/audio.wav。"""
    video_id = normalize_video_id(video_id)
    return resolve_in_dir(get_settings().audio_path, video_id, AUDIO_FILENAME)


def _has_audio_stream(video_path: Path) -> bool:
    """用 ffprobe 探测视频是否含音轨。"""
    ffprobe = _require_tool("ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioExtractTimeoutError(f"ffprobe 音轨探测超时 path={video_path}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 音轨探测失败: {result.stderr.strip()[-500:]}")
    return bool(result.stdout.strip())


def _probe_audio_recognizable(path: Path) -> None:
    """用 ffprobe 确认产物可识别为音频流。"""
    ffprobe = _require_tool("ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type,sample_rate,channels",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioExtractTimeoutError(f"ffprobe 音频校验超时 path={path}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise InvalidAudioError(
            f"ffprobe 无法识别音频文件 path={path}: {result.stderr.strip()[-500:]}"
        )


def validate_extracted_audio(path: Path) -> None:
    """校验音频非空、wave 可识别且为 16kHz 单声道；再经 ffprobe 确认可识别。"""
    if not path.is_file() or path.stat().st_size == 0:
        raise InvalidAudioError(f"音频文件为空或不存在 path={path}")
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            nframes = wav.getnframes()
    except wave.Error as exc:
        raise InvalidAudioError(f"wave 无法识别音频文件 path={path}: {exc}") from exc
    if channels != AUDIO_CHANNELS or rate != AUDIO_SAMPLE_RATE:
        raise InvalidAudioError(
            f"音频格式不符 path={path}: channels={channels} rate={rate}，"
            f"期望 {AUDIO_CHANNELS} 声道 {AUDIO_SAMPLE_RATE}Hz"
        )
    if nframes <= 0:
        raise InvalidAudioError(f"音频无采样帧 path={path}")
    _probe_audio_recognizable(path)


def is_valid_extracted_audio(path: Path) -> bool:
    """已有 audio.wav 是否可安全复用；工具缺失等运行时错误向上抛出，避免误删完好产物。"""
    try:
        validate_extracted_audio(path)
        return True
    except InvalidAudioError:
        return False


def extract_audio(video_id: str) -> Path:
    """从上传视频提取单声道 16kHz PCM wav 到 storage/audio/{video_id}/audio.wav。

    无音轨时抛 NoAudioTrackError，上传文件缺失时抛 UploadNotFoundError，
    超时抛 AudioExtractTimeoutError，ffmpeg 执行失败抛 RuntimeError。
    先写入临时文件，校验通过后原子替换正式产物。
    """
    video_id = normalize_video_id(video_id)
    video_path = find_uploaded_file(video_id)
    if not _has_audio_stream(video_path):
        raise NoAudioTrackError(f"视频不含音轨，无法执行语音识别 video_id={video_id}")

    ffmpeg = _require_tool("ffmpeg")
    dest = audio_file_path(video_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_name(AUDIO_FILENAME + ".tmp")
    tmp_dest.unlink(missing_ok=True)

    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i", str(video_path),
                "-vn",
                "-ac", str(AUDIO_CHANNELS),
                "-ar", str(AUDIO_SAMPLE_RATE),
                "-f", "wav",
                str(tmp_dest),
            ],
            capture_output=True,
            text=True,
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        tmp_dest.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise AudioExtractTimeoutError(
            f"ffmpeg 音频提取超时 video_id={video_id}"
        ) from exc

    if result.returncode != 0:
        tmp_dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg 音频提取失败 video_id={video_id}: {result.stderr.strip()[-500:]}"
        )
    try:
        validate_extracted_audio(tmp_dest)
        os.replace(tmp_dest, dest)
    except Exception:
        tmp_dest.unlink(missing_ok=True)
        raise
    logger.info("音频提取完成 video_id=%s path=%s", video_id, dest)
    return dest


def ensure_audio(video_id: str) -> Path:
    """返回 audio.wav 路径：有效产物直接复用，缺失或损坏则重新提取。"""
    video_id = normalize_video_id(video_id)
    dest = audio_file_path(video_id)
    if dest.is_file() and is_valid_extracted_audio(dest):
        return dest
    if dest.is_file():
        logger.warning("已有音频产物损坏或格式不符，将重新提取 path=%s", dest)
        dest.unlink(missing_ok=True)
    return extract_audio(video_id)
