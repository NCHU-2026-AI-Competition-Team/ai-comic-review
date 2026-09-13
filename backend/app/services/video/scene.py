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
from typing import Optional

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
# 极端时长视频的 pts_time 可能是科学计数法（1.23e+03）或边界浮点形式（.5、1.），
# 正则必须完整匹配整个数值并用负向先行断言锚定边界，避免把 1.23e+03 截断成 1.23
_PTS_TIME_PATTERN = re.compile(
    r"pts_time:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?![\d.])"
)

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


def detect_scene_changes(
    video_path: Path,
    threshold: float,
    max_boundaries: Optional[int] = None,
) -> list[int]:
    """检测镜头边界，返回按时间升序的毫秒时间戳列表（不含 0）。

    max_boundaries 用于检测阶段的资源保护：通过 -frames:v 让 ffmpeg 输出
    足够边界后提前退出，避免高切换频率视频的 showinfo 输出撑爆内存或拖垮超时；
    命中上限时结果可能被截断，记录告警。
    """
    ffmpeg = _require_tool("ffmpeg")
    command = [
        ffmpeg,
        "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
    ]
    if max_boundaries is not None:
        command += ["-frames:v", str(max_boundaries)]
    command += ["-f", "null", "-"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=DETECT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 镜头检测失败: {result.stderr.strip()[-500:]}")
    # showinfo 的解析结果输出在 stderr
    boundaries = [ms for ms in _parse_showinfo_pts(result.stderr) if ms > 0]
    if max_boundaries is not None and len(boundaries) >= max_boundaries:
        logger.warning(
            "镜头边界数达到检测上限 %d，超出部分已被截断 video=%s",
            max_boundaries, video_path,
        )
    return boundaries


# 组合 seek 的预定位秒数：输入级 -ss 先跳到目标前该秒数处（关键帧粒度），
# 输出级 -ss 再在解码流内精确补足，兼顾非均匀 GOP 视频的帧精度与解码耗时
_PRE_SEEK_SECONDS = 2.0


def _extract_single_frame(ffmpeg: str, video_path: Path, timestamp_ms: int, dest: Path) -> None:
    """用组合 seek 精确抽取指定毫秒时间戳的一帧。

    单纯输入级 -ss 在非均匀 GOP 视频上可能落在错误的关键帧区间造成帧偏移，
    这里输入级 -ss 快速定位到目标前 _PRE_SEEK_SECONDS 秒，输出级 -ss 精确偏移，
    保证抽到的帧与 timestamp_ms 严格对应。
    """
    target_seconds = timestamp_ms / 1000
    seek_base = max(0.0, target_seconds - _PRE_SEEK_SECONDS)
    seek_offset = target_seconds - seek_base
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss", f"{seek_base:.3f}",
            "-i", str(video_path),
            "-ss", f"{seek_offset:.3f}",
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

    # 检测阶段即施加帧数上限（起始帧占 1 个名额），防止高切换频率视频撑爆内存
    boundaries = detect_scene_changes(video_path, threshold, max_boundaries=max_frames - 1)
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
