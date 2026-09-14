"""模态事件落盘：OCR/ASR/后续 VLM 共用原子写与损坏恢复。"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.ids import normalize_video_id
from app.schemas.events import EventModality, ModalityEventsFile, TimelineEvent
from app.services.storage_paths import resolve_in_dir

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path, payload: object) -> Path:
    """先写同目录临时文件再 os.replace，避免半截 JSON 被下次当作有效结果复用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def modality_result_path(video_id: str, modality: EventModality) -> Path:
    """模态结果路径：storage/outputs/{video_id}/{modality}.json。"""
    video_id = normalize_video_id(video_id)
    return resolve_in_dir(get_settings().outputs_path, video_id, f"{modality}.json")


def write_modality_events(
    video_id: str, modality: EventModality, events: list[TimelineEvent]
) -> Path:
    """把模态事件原子写入 outputs/{video_id}/{modality}.json。"""
    video_id = normalize_video_id(video_id)
    path = modality_result_path(video_id, modality)
    payload = {
        "video_id": video_id,
        "modality": modality,
        "events": [event.model_dump(mode="json") for event in events],
    }
    return atomic_write_json(path, payload)


def try_load_modality_events(
    video_id: str, modality: EventModality
) -> Optional[list[TimelineEvent]]:
    """读取已有模态结果：不存在返回 None；损坏则删除后返回 None 以便重跑。"""
    video_id = normalize_video_id(video_id)
    path = modality_result_path(video_id, modality)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        payload = ModalityEventsFile.model_validate(data)
        if payload.video_id != video_id or payload.modality != modality:
            raise ValueError(
                f"事件文件与请求不一致：文件 video_id={payload.video_id} "
                f"modality={payload.modality}"
            )
        return payload.events
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.warning(
            "模态结果损坏，已删除并将重跑 video_id=%s modality=%s: %s",
            video_id, modality, exc,
        )
        path.unlink(missing_ok=True)
        return None
