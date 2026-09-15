"""OCR 流水线：对已完成抽帧的视频逐帧识别并产出统一时间线事件。

读取 storage/frames/{video_id}/frames.json，逐帧调用 OCR Provider
（ai.ocr 抽象，业务层不感知具体引擎），把每帧的文字行聚合为
TimelineEvent（modality='ocr'）：时间戳与帧的 timestamp_ms 对齐
（start_ms=end_ms），正文为各行文本拼接，置信度取各行均值，
逐行明细（文本/置信度/文本框）保留在 metadata.lines。
结果落盘 storage/outputs/{video_id}/ocr.json。

默认复用已有 ocr.json；force=True 才重跑。无文本帧不产生事件；
单帧识别失败记警告并跳过该帧，不中断整体；
frames.json 不存在或损坏明确抛错。
"""

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.core.config import ROOT_DIR, get_settings
from app.core.ids import normalize_video_id
from app.schemas.events import TimelineEvent
from app.schemas.video import FrameInfo, FramesInfo
from app.services.modality_store import (
    modality_result_path,
    try_load_modality_events,
    write_modality_events,
)
from app.services.storage_paths import resolve_in_dir
from app.services.video import FRAMES_JSON

# uvicorn 从 backend/ 启动时仓库根目录不在 sys.path，而 ai 包位于仓库根；
# 这里以最简方式保证 `import ai.ocr` 可用（pytest 则由 pytest.ini 的 pythonpath 覆盖）
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ai.ocr.base import OcrProvider, OcrTextLine  # noqa: E402
from ai.ocr.factory import get_default_provider  # noqa: E402

logger = logging.getLogger(__name__)

OCR_RESULT_FILENAME = "ocr.json"


class FramesNotFoundError(FileNotFoundError):
    """视频尚未完成抽帧（frames.json 不存在）。"""


class FramesCorruptedError(RuntimeError):
    """frames.json 内容损坏（非法 JSON 或字段校验失败）。"""


@dataclass
class OcrRunOutcome:
    """OCR 一次执行的结果：事件列表以及是否复用了已有 ocr.json。"""

    events: list[TimelineEvent]
    reused: bool = False


def load_frames_info(video_id: str) -> FramesInfo:
    """读取帧清单：不存在抛 FramesNotFoundError，损坏抛 FramesCorruptedError。"""
    video_id = normalize_video_id(video_id)
    frames_file = resolve_in_dir(get_settings().frames_path, video_id, FRAMES_JSON)
    if not frames_file.is_file():
        raise FramesNotFoundError(f"视频尚未完成抽帧 video_id={video_id}")
    try:
        data = json.loads(frames_file.read_text(encoding="utf-8"))
        return FramesInfo.model_validate(data)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("帧清单损坏 video_id=%s: %s", video_id, exc)
        raise FramesCorruptedError(f"帧清单文件损坏 video_id={video_id}") from exc


def _clamp_confidence(value: float, frame_id: str) -> float:
    """把引擎返回的置信度截断到 [0, 1]，越界时记警告（不中断流水线）。"""
    if 0.0 <= value <= 1.0:
        return value
    clamped = min(1.0, max(0.0, value))
    logger.warning(
        "OCR 置信度越界，已截断 frame_id=%s 原值=%s 截断后=%s", frame_id, value, clamped
    )
    return clamped


def frame_lines_to_event(
    video_id: str, frame: FrameInfo, lines: list[OcrTextLine]
) -> Optional[TimelineEvent]:
    """把单帧识别出的文字行聚合为 TimelineEvent，无文字行时返回 None。

    时间戳与帧严格对齐（start_ms=end_ms=timestamp_ms），保证多模态事件
    可基于同一时间轴融合；逐行明细保留在 metadata.lines 供追溯。
    置信度先逐行截断到 [0, 1] 再取均值，避免引擎异常值导致事件校验失败。
    """
    if not lines:
        return None
    clamped = [
        OcrTextLine(
            text=line.text,
            bbox=line.bbox,
            confidence=_clamp_confidence(line.confidence, frame.frame_id),
        )
        for line in lines
    ]
    return TimelineEvent(
        id=f"ocr-{frame.frame_id}",
        video_id=video_id,
        modality="ocr",
        start_ms=frame.timestamp_ms,
        end_ms=frame.timestamp_ms,
        content="\n".join(line.text for line in clamped),
        confidence=sum(line.confidence for line in clamped) / len(clamped),
        metadata={
            "frame_id": frame.frame_id,
            "lines": [
                {"text": line.text, "confidence": line.confidence, "box": line.bbox}
                for line in clamped
            ],
        },
    )


def ocr_result_path(video_id: str) -> Path:
    """OCR 结果文件路径：storage/outputs/{video_id}/ocr.json。"""
    return modality_result_path(video_id, "ocr")


def run_ocr(
    video_id: str,
    provider: Optional[OcrProvider] = None,
    force: bool = False,
) -> OcrRunOutcome:
    """对指定视频执行 OCR，聚合结果落盘 ocr.json 并返回执行结果。

    默认复用已有且完好的 ocr.json；force=True 时忽略已有结果重跑。
    provider 可注入以便测试替换；默认使用进程级惰性单例（工厂入口）。
    """
    video_id = normalize_video_id(video_id)
    if not force:
        existing = try_load_modality_events(video_id, "ocr")
        if existing is not None:
            logger.info(
                "OCR 复用已有结果 video_id=%s 事件数=%d", video_id, len(existing)
            )
            return OcrRunOutcome(events=existing, reused=True)

    frames_info = load_frames_info(video_id)
    if provider is None:
        provider = get_default_provider()
    frames_dir = resolve_in_dir(get_settings().frames_path, video_id)

    events = []
    failed = 0
    for frame in frames_info.frames:
        # frames.json 的 path 是 URL 形式，磁盘文件名取最后一段
        image_path = frames_dir / Path(frame.path).name
        try:
            lines = provider.recognize(image_path)
        except Exception as exc:
            failed += 1
            logger.warning(
                "单帧 OCR 识别失败，跳过该帧 video_id=%s frame_id=%s: %s",
                video_id, frame.frame_id, exc,
            )
            continue
        event = frame_lines_to_event(video_id, frame, lines)
        if event is not None:
            events.append(event)

    write_modality_events(video_id, "ocr", events)
    logger.info(
        "OCR 完成 video_id=%s 帧数=%d 事件数=%d 失败帧数=%d",
        video_id, len(frames_info.frames), len(events), failed,
    )
    return OcrRunOutcome(events=events, reused=False)
