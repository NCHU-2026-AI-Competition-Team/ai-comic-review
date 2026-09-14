"""视频上传与抽帧相关的数据模型。"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

VideoStatus = Literal["processed", "failed", "processing"]

# 上传时可选的采样模式：固定帧率 / 镜头切换检测
SamplingMode = Literal["fixed_fps", "scene"]

# frames.json 中记录的采样方式：与 SamplingMode 对应，scene 模式落盘为 scene_change
SamplingMethod = Literal["fixed_fps", "scene_change"]


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


class SamplingInfo(BaseModel):
    """采样方式描述：fixed_fps 只记录 fps，scene_change 只记录 threshold，二者互斥。"""

    method: SamplingMethod = Field(description="采样方式：fixed_fps 固定帧率 / scene_change 镜头切换检测")
    fps: Optional[float] = Field(default=None, description="固定帧率模式的抽帧帧率")
    threshold: Optional[float] = Field(default=None, description="镜头切换检测的场景分数阈值")

    @model_validator(mode="after")
    def _check_method_fields(self) -> "SamplingInfo":
        """method 与数值字段联动：固定帧率必须有 fps 且无 threshold，镜头检测反之。"""
        if self.method == "fixed_fps":
            if self.fps is None:
                raise ValueError("fixed_fps 采样必须提供 fps")
            if self.threshold is not None:
                raise ValueError("fixed_fps 采样不应携带 threshold")
        else:
            if self.threshold is None:
                raise ValueError("scene_change 采样必须提供 threshold")
            if self.fps is not None:
                raise ValueError("scene_change 采样不应携带 fps")
        return self


class FramesInfo(BaseModel):
    """抽帧结果汇总。"""

    sampling: SamplingInfo = Field(description="本次抽帧使用的采样方式")
    count: int = Field(ge=0, description="帧数量")
    frames: list[FrameInfo] = Field(default_factory=list, description="帧列表")


class VideoUploadResponse(BaseModel):
    """视频上传接口的响应模型。"""

    video_id: str
    filename: str
    status: VideoStatus
    sampling: SamplingMode
    metadata: Optional[VideoMetadata] = None
    frames: Optional[FramesInfo] = None


class VideoJob(BaseModel):
    """视频处理任务记录，落盘到 storage/uploads/{video_id}/job.json。"""

    video_id: str
    filename: str
    status: VideoStatus = "processing"
    sampling: SamplingMode = "fixed_fps"
    frame_fps: Optional[float] = Field(
        default=None,
        gt=0,
        description="任务级固定帧率抽帧覆盖；未提供时抽帧回退全局 FRAME_EXTRACTION_FPS",
    )
    scene_threshold: Optional[float] = Field(
        default=None,
        gt=0,
        lt=1,
        description="任务级镜头检测阈值覆盖；未提供时抽帧回退全局 SCENE_THRESHOLD",
    )
    scene_max_frames: Optional[int] = Field(
        default=None,
        gt=0,
        description="任务级镜头检测最大帧数覆盖；未提供时抽帧回退全局 SCENE_MAX_FRAMES",
    )
    error: Optional[str] = Field(default=None, description="处理失败时的错误信息")
    metadata: Optional[VideoMetadata] = None
    frames: Optional[FramesInfo] = None
    created_at: datetime = Field(default_factory=datetime.now)
