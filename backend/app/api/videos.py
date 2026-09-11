"""视频上传与处理状态查询路由。"""

import json
import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.schemas.video import FramesInfo, VideoJob, VideoUploadResponse
from app.services import registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/videos", tags=["videos"])

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv"}
ALLOWED_FRAME_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


def _validate_video_id(video_id: str) -> str:
    """校验 video_id 格式，防止路径穿越。"""
    if not VIDEO_ID_PATTERN.match(video_id):
        raise HTTPException(status_code=404, detail="视频不存在")
    return video_id


def _try_process(video_id: str) -> None:
    """尝试触发视频处理流水线。

    app.services.video.process_video 由后续 worker 实现，
    缺失（ImportError）时保持 processing 状态；其他异常记为 failed。
    """
    try:
        from app.services.video import process_video
    except ImportError:
        logger.info("视频处理模块尚未接入，video_id=%s 保持 processing", video_id)
        return
    try:
        process_video(video_id)
    except Exception as exc:
        logger.exception("视频处理失败 video_id=%s", video_id)
        registry.update_job(video_id, status="failed", error=str(exc))
        raise


@router.post("", response_model=VideoUploadResponse, status_code=201)
def upload_video(file: UploadFile) -> VideoUploadResponse:
    """上传视频：校验格式、保存原始文件、落盘任务记录并尝试触发处理。"""
    original_name = file.filename or ""
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的视频格式 '{ext}'，仅允许 {sorted(ALLOWED_EXTENSIONS)}",
        )

    settings = get_settings()
    video_id = str(uuid.uuid4())
    dest = settings.uploads_path / f"{video_id}{ext}"
    settings.uploads_path.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    job = registry.save_job(VideoJob(video_id=video_id, filename=original_name))

    try:
        _try_process(video_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"视频处理失败: {exc}") from exc

    job = registry.get_job(video_id) or job
    return VideoUploadResponse(
        video_id=job.video_id,
        filename=job.filename,
        status=job.status,
        metadata=job.metadata,
        frames=job.frames,
    )


@router.get("/{video_id}", response_model=VideoJob)
def get_video(video_id: str) -> VideoJob:
    """查询视频任务的当前状态与已有结果。"""
    _validate_video_id(video_id)
    job = registry.get_job(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    return job


@router.get("/{video_id}/frames", response_model=FramesInfo)
def get_video_frames(video_id: str) -> FramesInfo:
    """读取抽帧结果，尚未生成时返回 404。"""
    _validate_video_id(video_id)
    frames_file = get_settings().frames_path / video_id / "frames.json"
    if not frames_file.is_file():
        raise HTTPException(status_code=404, detail="抽帧结果尚未生成")
    try:
        data = json.loads(frames_file.read_text(encoding="utf-8"))
        return FramesInfo.model_validate(data)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("抽帧结果损坏 video_id=%s: %s", video_id, exc)
        raise HTTPException(status_code=500, detail="抽帧结果文件损坏") from exc


@router.get("/{video_id}/frames/{filename}")
def get_frame_image(video_id: str, filename: str) -> FileResponse:
    """按帧文件名访问帧图片。"""
    _validate_video_id(video_id)
    if Path(filename).name != filename or Path(filename).suffix.lower() not in ALLOWED_FRAME_SUFFIXES:
        raise HTTPException(status_code=404, detail="帧图片不存在")
    frame_file = get_settings().frames_path / video_id / filename
    if not frame_file.is_file():
        raise HTTPException(status_code=404, detail="帧图片不存在")
    return FileResponse(frame_file)
