"""视频上传与抽帧相关的数据模型。"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

VideoStatus = Literal["processed", "failed", "processing"]


class VideoMetadata(BaseModel):
    """视频元数据，当前阶段全部可选，由视频处理服务（app.services.video）填充。"""

    duration: Optional[float] = Field(default=None, description="时长（秒）")
    width: Optional[int] = Field(default=None, description="宽度（像素）")
    height: Optional[int] = Field(default=None, description="高度（像素）")
    fps: Optional[float] = Field(default=None, description="帧率")
    codec: Optional[str] = Field(default=None, description="视频编码")
    bitrate: Optional[int] = Field(default=None, description="码率（bps）")


class FrameInfo(BaseModel):
    """单帧信息。"""

    frame_id: str = Field(description="帧标识，对应帧图片文件名（不含扩展名）")
    timestamp_ms: int = Field(ge=0, description="时间戳（毫秒）")
    timestamp: str = Field(description="可读时间戳，如 00:01:23.456")
    path: str = Field(description="帧图片的访问路径，以 /api/videos/ 开头，可直接 GET 访问")


class FramesInfo(BaseModel):
    """抽帧结果汇总。"""

    count: int = Field(ge=0, description="帧数量")
    frames: list[FrameInfo] = Field(default_factory=list, description="帧列表")


class VideoUploadResponse(BaseModel):
    """视频上传接口的响应模型。"""

    video_id: str
    filename: str
    status: VideoStatus
    metadata: Optional[VideoMetadata] = None
    frames: Optional[FramesInfo] = None


class VideoJob(BaseModel):
    """视频处理任务记录，落盘到 storage/uploads/{video_id}/job.json。"""

    video_id: str
    filename: str
    status: VideoStatus = "processing"
    error: Optional[str] = Field(default=None, description="处理失败时的错误信息")
    metadata: Optional[VideoMetadata] = None
    frames: Optional[FramesInfo] = None
    created_at: datetime = Field(default_factory=datetime.now)
