"""视频处理流水线：ffprobe 元数据解析与抽帧。

process_video 由 app.api.videos 在上传成功后触发，
结果（metadata、frames）写回任务记录，帧清单落盘为 frames.json。
固定帧率抽帧由本模块实现，镜头切换检测见 app.services.video.scene。
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.schemas.video import FrameInfo, FramesInfo, SamplingInfo, SamplingMode, VideoMetadata
from app.services import registry

logger = logging.getLogger(__name__)

FRAME_FILENAME_PREFIX = "frame_"
FRAME_FILENAME_EXT = ".jpg"
FRAMES_JSON = "frames.json"


def _require_tool(name: str) -> str:
    """定位外部工具路径，缺失时抛出带指引的错误。"""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"未找到 {name}，请先安装 FFmpeg 并确保 {name} 在 PATH 中")
    return path


def _parse_fraction(value: str) -> Optional[float]:
    """解析 ffprobe 的分数形式帧率（如 "30000/1001"），无效时返回 None。"""
    if not value or value == "0/0":
        return None
    num, _, den = value.partition("/")
    try:
        numerator = float(num)
        denominator = float(den) if den else 1.0
    except ValueError:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def _safe_float(value: object) -> Optional[float]:
    """宽松解析浮点数，'N/A' 等非法值返回 None 而非抛异常。"""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _safe_int(value: object) -> Optional[int]:
    """宽松解析整数，'N/A' 等非法值返回 None 而非抛异常。"""
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _parse_probe_output(data: dict) -> VideoMetadata:
    """从 ffprobe 的 JSON 输出构建 VideoMetadata。"""
    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    fmt = data.get("format") or {}

    duration_raw = fmt.get("duration") or video_stream.get("duration")
    bitrate_raw = fmt.get("bit_rate") or video_stream.get("bit_rate")
    duration = _safe_float(duration_raw)

    fps = _parse_fraction(str(video_stream.get("avg_frame_rate", "")))
    if fps is None:
        fps = _parse_fraction(str(video_stream.get("r_frame_rate", "")))

    return VideoMetadata(
        duration=round(duration, 3) if duration is not None else None,
        width=_safe_int(video_stream.get("width")),
        height=_safe_int(video_stream.get("height")),
        fps=round(fps, 3) if fps is not None else None,
        codec=video_stream.get("codec_name"),
        bitrate=_safe_int(bitrate_raw),
    )


def probe_metadata(video_path: Path) -> VideoMetadata:
    """调用 ffprobe 解析视频元数据。"""
    ffprobe = _require_tool("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 解析失败: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 输出无法解析: {exc}") from exc
    return _parse_probe_output(data)


def _format_timestamp(timestamp_ms: int) -> str:
    """毫秒转 HH:MM:SS.mmm 可读时间戳。"""
    total_seconds, ms = divmod(timestamp_ms, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def extract_frames(video_id: str, video_path: Path, fps: float) -> FramesInfo:
    """按固定帧率抽帧到 storage/frames/{video_id}/，并写入 frames.json。"""
    ffmpeg = _require_tool("ffmpeg")
    out_dir = get_settings().frames_path / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / f"{FRAME_FILENAME_PREFIX}%06d{FRAME_FILENAME_EXT}")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i", str(video_path),
            "-vf", f"fps={fps}",
            "-q:v", "3",
            "-start_number", "0",
            pattern,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽帧失败: {result.stderr.strip()[-500:]}")

    frame_files = sorted(out_dir.glob(f"{FRAME_FILENAME_PREFIX}*{FRAME_FILENAME_EXT}"))
    frames = []
    for index, frame_file in enumerate(frame_files):
        # 按 index * 1000 / fps 计算，避免步长取整带来的累积误差
        timestamp_ms = round(index * 1000 / fps)
        frames.append(
            FrameInfo(
                frame_id=frame_file.stem,
                timestamp_ms=timestamp_ms,
                timestamp=_format_timestamp(timestamp_ms),
                path=f"/api/videos/{video_id}/frames/{frame_file.name}",
            )
        )
    info = FramesInfo(
        sampling=SamplingInfo(method="fixed_fps", fps=fps),
        count=len(frames),
        frames=frames,
    )
    (out_dir / FRAMES_JSON).write_text(
        json.dumps(info.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return info


def _find_uploaded_file(video_id: str) -> Path:
    """定位上传的原始视频文件。"""
    uploads = get_settings().uploads_path
    for candidate in sorted(uploads.glob(f"{video_id}.*")):
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"找不到 video_id={video_id} 对应的上传文件")


def process_video(video_id: str, sampling: SamplingMode = "fixed_fps") -> None:
    """处理已上传的视频：解析元数据、按采样模式抽帧并更新任务状态。"""
    settings = get_settings()
    video_path = _find_uploaded_file(video_id)

    metadata = probe_metadata(video_path)
    registry.update_job(video_id, metadata=metadata)
    logger.info("元数据解析完成 video_id=%s metadata=%s", video_id, metadata)

    if sampling == "scene":
        # 延迟导入避免包内循环依赖（scene 复用本模块的 ffmpeg 工具函数）
        from app.services.video.scene import extract_scene_frames

        frames = extract_scene_frames(video_id, video_path)
    else:
        frames = extract_frames(video_id, video_path, fps=settings.frame_extraction_fps)
    registry.update_job(video_id, status="processed", frames=frames)
    logger.info("抽帧完成 video_id=%s sampling=%s count=%d", video_id, sampling, frames.count)
