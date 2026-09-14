"""ASR 流水线：对视频提取音频并调用云端 ASR，产出统一时间线事件。

确保 storage/audio/{video_id}/audio.wav 存在（缺失或损坏时自动从上传视频提取），
调用 ASR Provider（ai.asr 抽象，业务层不感知具体引擎与部署形态）识别，
把每个分段聚合为 TimelineEvent（modality='asr'）：时间戳取分段的
start_ms/end_ms（int 毫秒，与全模态统一时间轴一致），正文为分段文本，
置信度截断到 [0, 1]；语言与分段序号保留在 metadata。
结果落盘 storage/outputs/{video_id}/asr.json。

默认复用已有 asr.json；force=True 才重跑。空文本分段不产生事件；
音频提取失败（无音轨、上传文件缺失）明确抛错。
"""

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.core.config import ROOT_DIR
from app.core.ids import normalize_video_id
from app.schemas.events import TimelineEvent
from app.services.audio import ensure_audio
from app.services.modality_store import (
    modality_result_path,
    try_load_modality_events,
    write_modality_events,
)

# uvicorn 从 backend/ 启动时仓库根目录不在 sys.path，而 ai 包位于仓库根；
# 这里以最简方式保证 `import ai.asr` 可用（pytest 则由 pytest.ini 的 pythonpath 覆盖）
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ai.asr.base import AsrProvider, AsrSegment  # noqa: E402
from ai.asr.factory import get_default_provider  # noqa: E402

logger = logging.getLogger(__name__)

ASR_RESULT_FILENAME = "asr.json"


@dataclass
class AsrRunOutcome:
    """ASR 一次执行的结果：事件列表以及是否复用了已有 asr.json。"""

    events: list[TimelineEvent]
    reused: bool = False


def _clamp_confidence(value: float, segment_index: int) -> float:
    """把引擎返回的置信度截断到 [0, 1]，越界时记警告（不中断流水线）。"""
    if 0.0 <= value <= 1.0:
        return value
    clamped = min(1.0, max(0.0, value))
    logger.warning(
        "ASR 置信度越界，已截断 segment=%d 原值=%s 截断后=%s", segment_index, value, clamped
    )
    return clamped


def segments_to_events(
    video_id: str, segments: list[AsrSegment], language: str
) -> list[TimelineEvent]:
    """把 ASR 分段聚合为 TimelineEvent 列表，空文本分段不产生事件。

    按 (start_ms, end_ms) 稳定排序后再产出事件，保证时间轴升序；
    metadata.segment_index 记录排序前的原始下标，便于对照引擎返回。
    时间戳直接取分段的 start_ms/end_ms（int 毫秒）；置信度截断到 [0, 1]。
    """
    video_id = normalize_video_id(video_id)
    ordered = sorted(
        enumerate(segments),
        key=lambda item: (item[1].start_ms, item[1].end_ms),
    )
    events = []
    for original_index, segment in ordered:
        if not segment.text.strip():
            continue
        events.append(
            TimelineEvent(
                id=f"asr-{original_index:06d}",
                video_id=video_id,
                modality="asr",
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                content=segment.text,
                confidence=_clamp_confidence(segment.confidence, original_index),
                metadata={"language": language, "segment_index": original_index},
            )
        )
    return events


def asr_result_path(video_id: str) -> Path:
    """ASR 结果文件路径：storage/outputs/{video_id}/asr.json。"""
    return modality_result_path(video_id, "asr")


def run_asr(
    video_id: str,
    provider: Optional[AsrProvider] = None,
    force: bool = False,
) -> AsrRunOutcome:
    """对指定视频执行 ASR，聚合结果落盘 asr.json 并返回执行结果。

    默认复用已有且完好的 asr.json；force=True 时忽略已有结果重跑。
    audio.wav 不存在或损坏时自动从上传视频提取（无音轨抛 NoAudioTrackError、
    上传缺失抛 UploadNotFoundError）；provider 可注入以便测试替换，
    默认使用进程级惰性单例（工厂入口）。
    """
    video_id = normalize_video_id(video_id)
    if not force:
        existing = try_load_modality_events(video_id, "asr")
        if existing is not None:
            logger.info(
                "ASR 复用已有结果 video_id=%s 事件数=%d", video_id, len(existing)
            )
            return AsrRunOutcome(events=existing, reused=True)

    audio_path = ensure_audio(video_id)
    if provider is None:
        provider = get_default_provider()
    result = provider.transcribe(audio_path)
    events = segments_to_events(video_id, result.segments, result.language)
    write_modality_events(video_id, "asr", events)
    logger.info(
        "ASR 完成 video_id=%s 分段数=%d 事件数=%d 语言=%s",
        video_id, len(result.segments), len(events), result.language,
    )
    return AsrRunOutcome(events=events, reused=False)
