"""人工复核结论落盘：与模态结果共用 outputs/{video_id}/ 目录与原子写。"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.ids import normalize_video_id
from app.schemas.verdict import HumanVerdict, StoredVerdict, VerdictRequest
from app.services.modality_store import atomic_write_json
from app.services.storage_paths import resolve_in_dir

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """生成带时区的 UTC 时间，序列化为 ISO8601。"""
    return datetime.now(timezone.utc)


def verdict_path(video_id: str) -> Path:
    """复核结论路径：storage/outputs/{video_id}/verdict.json。"""
    video_id = normalize_video_id(video_id)
    return resolve_in_dir(get_settings().outputs_path, video_id, "verdict.json")


def try_load_stored_verdict(video_id: str) -> Optional[StoredVerdict]:
    """读取落盘记录：不存在返回 None；损坏则删除后返回 None 以便覆盖写入。"""
    video_id = normalize_video_id(video_id)
    path = verdict_path(video_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        payload = StoredVerdict.model_validate(data)
        if payload.video_id != video_id:
            raise ValueError(
                f"复核文件与请求不一致：文件 video_id={payload.video_id}"
            )
        return payload
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.warning("复核结论损坏，已删除 video_id=%s: %s", video_id, exc)
        path.unlink(missing_ok=True)
        return None


def try_load_verdict(video_id: str) -> Optional[HumanVerdict]:
    """读取对外公开的复核结论（不含内部 updated_at）；未提交或损坏返回 None。"""
    stored = try_load_stored_verdict(video_id)
    if stored is None:
        return None
    return HumanVerdict.model_validate(stored.model_dump())


def save_verdict(video_id: str, request: VerdictRequest) -> HumanVerdict:
    """覆盖写入复核结论：首次记录 created_at，重复提交保留 created_at 并刷新 updated_at。"""
    video_id = normalize_video_id(video_id)
    existing = try_load_stored_verdict(video_id)
    now = _now()
    record = StoredVerdict(
        video_id=video_id,
        decision=request.decision,
        note=request.note,
        event_id=request.event_id,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
    )
    path = verdict_path(video_id)
    atomic_write_json(path, record.model_dump(mode="json"))
    return HumanVerdict.model_validate(record.model_dump())
