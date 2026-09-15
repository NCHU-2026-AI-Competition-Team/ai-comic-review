"""历史任务列表摘要 schema：供 GET /api/videos 使用。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.report import ModalityRunStatus
from app.schemas.verdict import VerdictDecision
from app.schemas.video import VideoMetadata, VideoStatus


class VideoListVerdict(BaseModel):
    """列表中的复核摘要：仅公开 decision / note / created_at。"""

    decision: VerdictDecision = Field(description="复核决定：approve / reject / false_positive")
    note: str = Field(description="复核备注")
    created_at: datetime = Field(description="首次提交时间，ISO8601")


class VideoListModalities(BaseModel):
    """各模态是否已落盘及事件数量。"""

    ocr: ModalityRunStatus
    asr: ModalityRunStatus
    vlm: ModalityRunStatus


class VideoListItem(BaseModel):
    """历史任务列表中的单条摘要。"""

    video_id: str
    filename: str
    status: VideoStatus
    created_at: datetime
    metadata: Optional[VideoMetadata] = None
    modalities: VideoListModalities = Field(description="ocr / asr / vlm 运行状态")
    verdict: Optional[VideoListVerdict] = Field(
        default=None, description="人工复核摘要；未提交或损坏时为 null"
    )
