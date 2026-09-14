"""上传文件定位：按允许扩展名枚举，禁止自由 glob。"""

from pathlib import Path

from app.core.config import get_settings
from app.core.ids import normalize_video_id
from app.services.storage_paths import resolve_in_dir

ALLOWED_VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv")


class UploadNotFoundError(FileNotFoundError):
    """找不到 video_id 对应的上传视频文件。"""


def find_uploaded_file(video_id: str) -> Path:
    """按允许扩展名枚举 storage/uploads/{uuid}.mp4|.mov|.mkv，缺失时抛 UploadNotFoundError。"""
    video_id = normalize_video_id(video_id)
    uploads = get_settings().uploads_path
    for ext in ALLOWED_VIDEO_EXTENSIONS:
        candidate = resolve_in_dir(uploads, f"{video_id}{ext}")
        if candidate.is_file():
            return candidate
    raise UploadNotFoundError(f"找不到 video_id={video_id} 对应的上传文件")
