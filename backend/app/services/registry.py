"""视频任务记录的文件注册表。

当前以 storage/uploads/{video_id}/job.json 落盘；
接口保持独立，后续可平滑替换为 PostgreSQL 实现。
"""

import json
import logging
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.schemas.video import VideoJob

logger = logging.getLogger(__name__)

JOB_FILENAME = "job.json"


class JobCorruptedError(RuntimeError):
    """任务记录文件存在但内容损坏（非法 JSON 或字段校验失败）。"""


def _job_dir(video_id: str) -> Path:
    return get_settings().uploads_path / video_id


def _job_path(video_id: str) -> Path:
    return _job_dir(video_id) / JOB_FILENAME


def save_job(job: VideoJob) -> VideoJob:
    """创建或覆盖任务记录。"""
    job_dir = _job_dir(job.video_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    _job_path(job.video_id).write_text(
        json.dumps(job.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return job


def get_job(video_id: str) -> Optional[VideoJob]:
    """读取任务记录：文件不存在返回 None；文件损坏抛出 JobCorruptedError。"""
    path = _job_path(video_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return VideoJob.model_validate(data)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("任务记录损坏 video_id=%s: %s", video_id, exc)
        raise JobCorruptedError(f"任务记录文件损坏 video_id={video_id}") from exc


def update_job(video_id: str, **fields: object) -> Optional[VideoJob]:
    """更新任务记录的指定字段并落盘，记录不存在时返回 None。"""
    job = get_job(video_id)
    if job is None:
        return None
    job = job.model_copy(update=fields)
    return save_job(job)
