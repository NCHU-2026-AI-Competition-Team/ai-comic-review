"""VLM 审核流水线：组装已抽帧 + OCR/ASR 结果调用主审，产出统一时间线事件。

读取 storage/frames/{video_id}/frames.json，按配置的单次最大帧数分批，
把已落盘的 OCR/ASR 文本与帧时间戳组装为云端 /review 契约请求，
调用 VLM Provider（ai.vlm 抽象，业务层不感知具体引擎）。

主审结果按帧时间窗展开为 TimelineEvent（modality='vlm'）：
有风险时对落入 [start_ms, end_ms] 的帧各产一条事件；
无风险时每批产一条覆盖该批时间窗的事件。
契约字段（risk/category/severity/evidence/reason/suggestion 等）写入 metadata。

8B 低置信 / 高风险 / JSON 不合格 / 模态冲突时尝试 32B 复审；
32B 未部署（501）时保留 8B 结果并标注待复审，不中断主链路。
默认复用已有 vlm.json；force=True 才重跑。结果经 modality_store 原子落盘。
"""

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.core.config import ROOT_DIR, get_settings
from app.core.ids import normalize_video_id
from app.schemas.events import EscalationStatus, TimelineEvent
from app.schemas.report import ModalityRunStatus, OverallConclusion, ReviewReport
from app.schemas.video import FrameInfo
from app.services import registry
from app.services.modality_store import (
    modality_result_path,
    try_load_modality_events,
    write_modality_events,
)
from app.services.ocr_pipeline import (
    FramesCorruptedError,
    FramesNotFoundError,
    load_frames_info,
)
from app.services.storage_paths import resolve_in_dir

# uvicorn 从 backend/ 启动时仓库根目录不在 sys.path，而 ai 包位于仓库根
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ai.vlm.base import (  # noqa: E402
    VlmEscalationUnavailableError,
    VlmProvider,
    VlmProviderError,
    VlmResponseFormatError,
    VlmReviewInput,
)
from ai.vlm.escalate import (  # noqa: E402
    FusedVerdict,
    fuse_overall,
    should_escalate,
)
from ai.vlm.factory import get_default_provider  # noqa: E402
from ai.vlm.schemas import MAX_TEXT_FIELD_CHARS, VlmReviewResult  # noqa: E402

logger = logging.getLogger(__name__)

VLM_RESULT_FILENAME = "vlm.json"
_SEVERITY_SORT = {"high": 0, "medium": 1, "low": 2, "none": 3}


@dataclass
class VlmRunOutcome:
    """VLM 一次执行的结果：事件列表、是否复用、以及 escalation 状态。"""

    events: list[TimelineEvent]
    reused: bool = False
    needs_escalation: bool = False
    escalation_status: EscalationStatus = "not_needed"


def vlm_result_path(video_id: str) -> Path:
    """VLM 结果文件路径：storage/outputs/{video_id}/vlm.json。"""
    return modality_result_path(video_id, "vlm")


def _clamp_confidence(value: float) -> float:
    if 0.0 <= value <= 1.0:
        return value
    return min(1.0, max(0.0, value))


def _truncate(text: str, limit: int = MAX_TEXT_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _events_overlap_window(
    events: list[TimelineEvent], start_ms: int, end_ms: int
) -> list[TimelineEvent]:
    matched = []
    for event in events:
        if event.end_ms < start_ms or event.start_ms > end_ms:
            continue
        matched.append(event)
    return matched


def join_modality_text(
    events: list[TimelineEvent], start_ms: int, end_ms: int
) -> str:
    """把落入时间窗的模态事件拼成契约文本字段。"""
    parts = []
    for event in _events_overlap_window(events, start_ms, end_ms):
        text = event.content.strip()
        if not text:
            continue
        parts.append(f"[{event.start_ms}-{event.end_ms}ms] {text}")
    return _truncate("\n".join(parts))


def _build_context(video_id: str) -> str:
    job = registry.get_job(video_id)
    if job is None:
        return ""
    parts = [f"文件名={job.filename}"]
    meta = job.metadata
    if meta is not None:
        if meta.duration is not None:
            parts.append(f"时长={meta.duration}s")
        if meta.width is not None and meta.height is not None:
            parts.append(f"分辨率={meta.width}x{meta.height}")
    return " ".join(parts)


def _placeholder_result(batch: list[FrameInfo], reason: str) -> VlmReviewResult:
    start_ms = batch[0].timestamp_ms if batch else 0
    end_ms = batch[-1].timestamp_ms if batch else 0
    if end_ms < start_ms:
        end_ms = start_ms
    return VlmReviewResult(
        risk=True,
        category="other",
        severity="medium",
        confidence=0.0,
        start_ms=start_ms,
        end_ms=end_ms,
        evidence="",
        reason=reason,
        suggestion="待复审",
    )


def _review_with_escalation(
    provider: VlmProvider,
    primary_payload: VlmReviewInput,
    batch: list[FrameInfo],
    *,
    low_confidence_threshold: float,
    escalation_model: str,
) -> tuple[VlmReviewResult, EscalationStatus, tuple[str, ...]]:
    """先走 8B 主审；需要时尝试 32B，501 则保留 8B 并标注待复审。"""
    json_invalid = False
    primary: VlmReviewResult | None = None
    try:
        primary = provider.review(primary_payload)
    except VlmResponseFormatError:
        json_invalid = True
        logger.warning("主审 JSON 不合格，将尝试 escalation")
    except VlmEscalationUnavailableError:
        # 主审误配成 32B 标识时也会 501：降级为待复审占位，不阻断
        placeholder = _placeholder_result(
            batch, "主审模型不可用（可能误用未部署的复审标识），已标记待复审"
        )
        return placeholder, "pending_review", ("json_invalid",)

    decision = should_escalate(
        primary,
        json_invalid=json_invalid,
        ocr_text=primary_payload.ocr_text,
        asr_text=primary_payload.asr_text,
        low_confidence_threshold=low_confidence_threshold,
    )
    if not decision.needs_escalation:
        assert primary is not None
        return primary, "not_needed", ()

    escalate_payload = VlmReviewInput(
        frame_paths=primary_payload.frame_paths,
        frame_timestamps_ms=primary_payload.frame_timestamps_ms,
        ocr_text=primary_payload.ocr_text,
        asr_text=primary_payload.asr_text,
        context=primary_payload.context,
        rules=primary_payload.rules,
        model=escalation_model,
    )
    try:
        escalated = provider.review(escalate_payload)
        return escalated, "completed", decision.reasons
    except VlmProviderError as exc:
        # 501 / 超时 / 格式错误均不得阻断主链路：保留 8B（或占位）并标注待复审
        logger.info("复审不可用或失败，保留主审结果并标注待复审 reasons=%s err=%s", decision.reasons, exc)
        if primary is not None:
            return primary, "pending_review", decision.reasons
        placeholder = _placeholder_result(
            batch, "主审输出不合规且复审不可用，已标记待复审"
        )
        return placeholder, "pending_review", decision.reasons


def expand_review_to_events(
    video_id: str,
    batch_index: int,
    batch: list[FrameInfo],
    result: VlmReviewResult,
    *,
    model: str,
    escalation_status: EscalationStatus,
    escalation_reasons: tuple[str, ...],
) -> list[TimelineEvent]:
    """把主审结果按帧时间窗展开为 TimelineEvent 列表。"""
    video_id = normalize_video_id(video_id)
    frame_ids = [frame.frame_id for frame in batch]
    needs_escalation = escalation_status == "pending_review"
    base_meta = {
        "risk": result.risk,
        "category": result.category,
        "severity": result.severity,
        "evidence": result.evidence,
        "reason": result.reason,
        "suggestion": result.suggestion,
        "needs_escalation": needs_escalation,
        "escalation_status": escalation_status,
        "escalation_reasons": list(escalation_reasons),
        "model": model,
        "batch_index": batch_index,
        "review_start_ms": result.start_ms,
        "review_end_ms": result.end_ms,
    }
    content = result.evidence.strip() or result.reason.strip() or result.suggestion
    confidence = _clamp_confidence(result.confidence)

    if result.risk:
        matched = [
            frame
            for frame in batch
            if result.start_ms <= frame.timestamp_ms <= result.end_ms
        ]
        if not matched:
            return [
                TimelineEvent(
                    id=f"vlm-{batch_index:04d}",
                    video_id=video_id,
                    modality="vlm",
                    start_ms=result.start_ms,
                    end_ms=result.end_ms,
                    content=content,
                    confidence=confidence,
                    metadata={**base_meta, "frame_ids": frame_ids},
                )
            ]
        events = []
        for frame in matched:
            events.append(
                TimelineEvent(
                    id=f"vlm-{batch_index:04d}-{frame.frame_id}",
                    video_id=video_id,
                    modality="vlm",
                    start_ms=frame.timestamp_ms,
                    end_ms=frame.timestamp_ms,
                    content=content,
                    confidence=confidence,
                    metadata={**base_meta, "frame_ids": [frame.frame_id]},
                )
            )
        return events

    start_ms = batch[0].timestamp_ms if batch else 0
    end_ms = batch[-1].timestamp_ms if batch else 0
    if end_ms < start_ms:
        end_ms = start_ms
    return [
        TimelineEvent(
            id=f"vlm-{batch_index:04d}",
            video_id=video_id,
            modality="vlm",
            start_ms=start_ms,
            end_ms=end_ms,
            content=content,
            confidence=confidence,
            metadata={**base_meta, "frame_ids": frame_ids},
        )
    ]


def _status_from_events(events: list[TimelineEvent]) -> tuple[bool, EscalationStatus]:
    statuses = [str(event.metadata.get("escalation_status", "not_needed")) for event in events]
    if any(status == "pending_review" for status in statuses):
        return True, "pending_review"
    if any(status == "completed" for status in statuses):
        return False, "completed"
    needs = any(bool(event.metadata.get("needs_escalation")) for event in events)
    if needs:
        return True, "pending_review"
    return False, "not_needed"


def _collect_results_from_events(events: list[TimelineEvent]) -> list[VlmReviewResult]:
    results: list[VlmReviewResult] = []
    seen_batches: set[int] = set()
    for event in events:
        batch_index = event.metadata.get("batch_index")
        if isinstance(batch_index, int):
            if batch_index in seen_batches:
                continue
            seen_batches.add(batch_index)
        try:
            results.append(
                VlmReviewResult(
                    risk=bool(event.metadata.get("risk", False)),
                    category=str(event.metadata.get("category", "none")),  # type: ignore[arg-type]
                    severity=str(event.metadata.get("severity", "none")),  # type: ignore[arg-type]
                    confidence=_clamp_confidence(event.confidence),
                    start_ms=int(event.metadata.get("review_start_ms", event.start_ms)),
                    end_ms=int(event.metadata.get("review_end_ms", event.end_ms)),
                    evidence=str(event.metadata.get("evidence", "")),
                    reason=str(event.metadata.get("reason", event.content)),
                    suggestion=str(event.metadata.get("suggestion", "")),
                )
            )
        except Exception:
            logger.warning("跳过无法还原的 VLM 事件 id=%s", event.id)
    return results


def run_review(
    video_id: str,
    provider: Optional[VlmProvider] = None,
    force: bool = False,
) -> VlmRunOutcome:
    """对指定视频执行 VLM 主审，聚合结果落盘 vlm.json 并返回执行结果。

    默认复用已有且完好的 vlm.json；force=True 时忽略已有结果重跑。
    frames.json 不存在抛 FramesNotFoundError，损坏抛 FramesCorruptedError。
    provider 可注入以便测试替换，默认使用进程级惰性单例（工厂入口）。
    """
    video_id = normalize_video_id(video_id)
    frames_info = load_frames_info(video_id)
    if not force:
        existing = try_load_modality_events(video_id, "vlm")
        if existing is not None:
            needs, status = _status_from_events(existing)
            logger.info(
                "VLM 复用已有结果 video_id=%s 事件数=%d needs_escalation=%s",
                video_id,
                len(existing),
                needs,
            )
            return VlmRunOutcome(
                events=existing,
                reused=True,
                needs_escalation=needs,
                escalation_status=status,
            )

    settings = get_settings()
    if provider is None:
        provider = get_default_provider()

    ocr_events = try_load_modality_events(video_id, "ocr") or []
    asr_events = try_load_modality_events(video_id, "asr") or []
    context = _truncate(_build_context(video_id))
    rules = settings.vlm_review_rules
    frames_dir = resolve_in_dir(get_settings().frames_path, video_id)
    max_frames = settings.vlm_max_frames_per_request

    events: list[TimelineEvent] = []
    overall_status: EscalationStatus = "not_needed"
    overall_needs = False

    frames = list(frames_info.frames)
    if not frames:
        write_modality_events(video_id, "vlm", [])
        return VlmRunOutcome(events=[], reused=False)

    for batch_index, offset in enumerate(range(0, len(frames), max_frames)):
        batch = frames[offset : offset + max_frames]
        batch_paths = [frames_dir / Path(frame.path).name for frame in batch]
        batch_start = batch[0].timestamp_ms
        batch_end = batch[-1].timestamp_ms
        payload = VlmReviewInput(
            frame_paths=batch_paths,
            frame_timestamps_ms=[frame.timestamp_ms for frame in batch],
            ocr_text=join_modality_text(ocr_events, batch_start, batch_end),
            asr_text=join_modality_text(asr_events, batch_start, batch_end),
            context=context,
            rules=rules,
            model=settings.vlm_primary,
        )
        result, status, reasons = _review_with_escalation(
            provider,
            payload,
            batch,
            low_confidence_threshold=settings.vlm_low_confidence_threshold,
            escalation_model=settings.vlm_escalation,
        )
        if status == "pending_review":
            overall_needs = True
            overall_status = "pending_review"
        elif status == "completed" and overall_status == "not_needed":
            overall_status = "completed"
        event_model = settings.vlm_escalation if status == "completed" else settings.vlm_primary
        events.extend(
            expand_review_to_events(
                video_id,
                batch_index,
                batch,
                result,
                model=event_model,
                escalation_status=status,
                escalation_reasons=reasons,
            )
        )

    write_modality_events(video_id, "vlm", events)
    if overall_status == "pending_review":
        overall_needs = True
    logger.info(
        "VLM 审核完成 video_id=%s 帧数=%d 事件数=%d needs_escalation=%s status=%s",
        video_id,
        len(frames),
        len(events),
        overall_needs,
        overall_status,
    )
    return VlmRunOutcome(
        events=events,
        reused=False,
        needs_escalation=overall_needs,
        escalation_status=overall_status,
    )


def _sort_risk_events(events: list[TimelineEvent]) -> list[TimelineEvent]:
    risky = [event for event in events if event.metadata.get("risk") is True]
    risky.sort(
        key=lambda event: (
            _SEVERITY_SORT.get(str(event.metadata.get("severity", "none")), 9),
            -event.confidence,
            event.start_ms,
        )
    )
    return risky


def _modality_status(video_id: str, modality: str) -> ModalityRunStatus:
    events = try_load_modality_events(video_id, modality)  # type: ignore[arg-type]
    if events is None:
        path = modality_result_path(video_id, modality)  # type: ignore[arg-type]
        return ModalityRunStatus(ran=path.is_file(), event_count=0)
    return ModalityRunStatus(ran=True, event_count=len(events))


def _verdict_to_overall(verdict: FusedVerdict) -> OverallConclusion:
    status: EscalationStatus = verdict.escalation_status  # type: ignore[assignment]
    if status not in {"not_needed", "pending_review", "completed"}:
        status = "pending_review" if verdict.needs_escalation else "not_needed"
    return OverallConclusion(
        risk=verdict.risk,
        category=verdict.category,
        severity=verdict.severity,
        confidence=_clamp_confidence(verdict.confidence),
        suggestion=verdict.suggestion,
        reason=verdict.reason,
        needs_escalation=verdict.needs_escalation,
        escalation_status=status,
    )


def build_review_report(video_id: str, vlm_events: list[TimelineEvent]) -> ReviewReport:
    """根据任务记录与各模态落盘结果构建结构化审核报告。"""
    video_id = normalize_video_id(video_id)
    job = registry.get_job(video_id)
    if job is None:
        raise FileNotFoundError(f"视频不存在 video_id={video_id}")

    ocr_events = try_load_modality_events(video_id, "ocr") or []
    asr_events = try_load_modality_events(video_id, "asr") or []
    needs, status = _status_from_events(vlm_events)
    ocr_text = join_modality_text(ocr_events, 0, 2**31 - 1)
    asr_text = join_modality_text(asr_events, 0, 2**31 - 1)
    verdict = fuse_overall(
        _collect_results_from_events(vlm_events),
        needs_escalation=needs,
        escalation_status=status,
        ocr_text=ocr_text,
        asr_text=asr_text,
    )
    return ReviewReport(
        video_id=video_id,
        filename=job.filename,
        status=job.status,
        sampling=job.sampling,
        metadata=job.metadata,
        modalities={
            "ocr": _modality_status(video_id, "ocr"),
            "asr": _modality_status(video_id, "asr"),
            "vlm": ModalityRunStatus(ran=True, event_count=len(vlm_events)),
        },
        risk_events=_sort_risk_events(vlm_events),
        overall=_verdict_to_overall(verdict),
    )
