"""音频提取：从已上传视频提取 ASR 所需的单声道 16kHz PCM wav。

对照 services/video 包的模式：以 subprocess.run 参数列表形式调用
ffprobe/ffmpeg（无 shell），工具缺失、无音轨与提取失败分别给出明确错误。
产物落盘 storage/audio/{video_id}/audio.wav，供 ai.asr Provider 上传识别；
采样率与声道数由 ASR 服务契约固定（16kHz 单声道 PCM wav），不走配置。
"""

import logging
import subprocess
from pathlib import Path

from app.core.config import get_settings
from app.services.video import _require_tool

logger = logging.getLogger(__name__)

AUDIO_FILENAME = "audio.wav"
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
PROBE_TIMEOUT_SECONDS = 60
EXTRACT_TIMEOUT_SECONDS = 300


class UploadNotFoundError(FileNotFoundError):
    """找不到 video_id 对应的上传视频文件。"""


class NoAudioTrackError(RuntimeError):
    """视频不含音轨，无法执行 ASR。"""


def _find_uploaded_file(video_id: str) -> Path:
    """定位上传的原始视频文件，缺失时抛 UploadNotFoundError。"""
    uploads = get_settings().uploads_path
    for candidate in sorted(uploads.glob(f"{video_id}.*")):
        if candidate.is_file():
            return candidate
    raise UploadNotFoundError(f"找不到 video_id={video_id} 对应的上传文件")


def _has_audio_stream(video_path: Path) -> bool:
    """用 ffprobe 探测视频是否含音轨。"""
    ffprobe = _require_tool("ffprobe")
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
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 音轨探测失败: {result.stderr.strip()[-500:]}")
    return bool(result.stdout.strip())


def audio_file_path(video_id: str) -> Path:
    """音频产物路径：storage/audio/{video_id}/audio.wav。"""
    return get_settings().audio_path / video_id / AUDIO_FILENAME


def extract_audio(video_id: str) -> Path:
    """从上传视频提取单声道 16kHz PCM wav 到 storage/audio/{video_id}/audio.wav。

    无音轨时抛 NoAudioTrackError，上传文件缺失时抛 UploadNotFoundError，
    ffmpeg 执行失败抛 RuntimeError。
    """
    video_path = _find_uploaded_file(video_id)
    if not _has_audio_stream(video_path):
        raise NoAudioTrackError(f"视频不含音轨，无法执行语音识别 video_id={video_id}")

    ffmpeg = _require_tool("ffmpeg")
    dest = audio_file_path(video_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i", str(video_path),
            "-vn",
            "-ac", str(AUDIO_CHANNELS),
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-f", "wav",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg 音频提取失败 video_id={video_id}: {result.stderr.strip()[-500:]}")
    if not dest.is_file():
        raise RuntimeError(f"ffmpeg 未产出音频文件 video_id={video_id}: {dest}")
    logger.info("音频提取完成 video_id=%s path=%s", video_id, dest)
    return dest


def ensure_audio(video_id: str) -> Path:
    """返回 audio.wav 路径，不存在时自动执行提取。"""
    dest = audio_file_path(video_id)
    if dest.is_file():
        return dest
    return extract_audio(video_id)
