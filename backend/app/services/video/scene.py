"""镜头切换检测：基于 ffmpeg 场景分数识别镜头边界并精确抽帧。

检测阶段用 select='gt(scene,THRESHOLD)' 筛出场景分数超过阈值的帧，
从 showinfo 输出解析其 pts_time 得到毫秒级边界时间戳；
抽帧阶段对每个边界用 ffmpeg -ss 精确截取一帧，
并额外保留视频起始帧（t=0）作为首个镜头的代表帧。
"""

import json
import logging
import re
import subprocess
from pathlib import Path

from app.core.config import get_settings
from app.schemas.video import FrameInfo, FramesInfo, SamplingInfo
from app.services.video import (
    FRAME_FILENAME_EXT,
    FRAME_FILENAME_PREFIX,
    FRAMES_JSON,
    _format_timestamp,
    _require_tool,
)

logger = logging.getLogger(__name__)

# showinfo 输出形如 "[Parsed_showinfo_1 @ ...] n: 1 pts: 60 pts_time:2.000000 ..."
# 用正则提取 pts_time 浮点值，避免依赖固定列位置的脆弱截断
_PTS_TIME_PATTERN = re.compile(r"pts_time:\s*(-?\d+(?:\.\d+)?)")

DETECT_TIMEOUT_SECONDS = 300
EXTRACT_TIMEOUT_SECONDS = 60


def _parse_showinfo_pts(stderr_text: str) -> list[int]:
    """从 ffmpeg showinfo 输出中解析命中帧的时间戳，返回去重排序后的毫秒列表。"""
    timestamps_ms = []
    for line in stderr_text.splitlines():
        if "showinfo" not in line:
            continue
        match = _PTS_TIME_PATTERN.search(line)
        if match:
            timestamps_ms.append(round(float(match.group(1)) * 1000))
    return sorted(set(timestamps_ms))


def detect_scene_changes(video_path: Path, threshold: float) -> list[int]:
    """检测镜头边界，返回按时间升序的毫秒时间戳列表（不含 0）。"""
    ffmpeg = _require_tool("ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-i", str(video_path),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=DETECT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 镜头检测失败: {result.stderr.strip()[-500:]}")
    # showinfo 的解析结果输出在 stderr
    return [ms for ms in _parse_showinfo_pts(result.stderr) if ms > 0]


def _extract_single_frame(ffmpeg: str, video_path: Path, timestamp_ms: int, dest: Path) -> None:
    """用 ffmpeg -ss 精确抽取指定毫秒时间戳的一帧。"""
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss", f"{timestamp_ms / 1000:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "3",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽帧失败 timestamp_ms={timestamp_ms}: {result.stderr.strip()[-500:]}")
    if not dest.is_file():
        raise RuntimeError(f"ffmpeg 未产出帧图片 timestamp_ms={timestamp_ms}: {dest}")


def extract_scene_frames(video_id: str, video_path: Path) -> FramesInfo:
    """按镜头切换检测抽帧到 storage/frames/{video_id}/，并写入 frames.json。"""
    settings = get_settings()
    threshold = settings.scene_threshold
    max_frames = settings.scene_max_frames

    boundaries = detect_scene_changes(video_path, threshold)
    # 起始帧固定纳入，作为首个镜头的代表帧
    timestamps = [0, *boundaries]
    if len(timestamps) > max_frames:
        logger.warning(
            "镜头数量 %d 超过上限 %d，超出部分截断 video_id=%s",
            len(timestamps), max_frames, video_id,
        )
        timestamps = timestamps[:max_frames]

    ffmpeg = _require_tool("ffmpeg")
    out_dir = settings.frames_path / video_id
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for index, timestamp_ms in enumerate(timestamps):
        frame_file = out_dir / f"{FRAME_FILENAME_PREFIX}{index:06d}{FRAME_FILENAME_EXT}"
        _extract_single_frame(ffmpeg, video_path, timestamp_ms, frame_file)
        frames.append(
            FrameInfo(
                frame_id=frame_file.stem,
                timestamp_ms=timestamp_ms,
                timestamp=_format_timestamp(timestamp_ms),
                path=f"/api/videos/{video_id}/frames/{frame_file.name}",
            )
        )

    info = FramesInfo(
        sampling=SamplingInfo(method="scene_change", threshold=threshold),
        count=len(frames),
        frames=frames,
    )
    (out_dir / FRAMES_JSON).write_text(
        json.dumps(info.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return info
