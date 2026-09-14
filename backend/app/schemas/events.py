"""多模态统一时间线事件 schema。"""

from typing import Any, Literal

from pydantic import BaseModel, Field

# 事件来源模态：全后端唯一定义，TimelineEvent.modality 与各接口共用；
# 前端 frontend/src/types.ts 的 EventModality 须与此保持一致（手工同步）
EventModality = Literal["ocr", "asr", "vision", "vlm"]


class TimelineEvent(BaseModel):
    """统一时间线事件：未来 OCR/ASR/视觉/VLM 各模态的产出均归一到该结构。"""

    id: str = Field(description="事件唯一标识")
    video_id: str = Field(description="所属视频标识")
    modality: EventModality = Field(description="事件来源模态")
    start_ms: int = Field(ge=0, description="事件起始时间（毫秒）")
    end_ms: int = Field(ge=0, description="事件结束时间（毫秒）")
    content: str = Field(description="事件内容（文本或描述）")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度")
    metadata: dict[str, Any] = Field(default_factory=dict, description="模态特定的附加信息")


class ModalityEventsFile(BaseModel):
    """模态事件落盘文件（storage/outputs/{video_id}/{modality}.json）的顶层结构。"""

    video_id: str = Field(description="所属视频标识")
    modality: EventModality = Field(description="事件来源模态")
    events: list[TimelineEvent] = Field(description="事件列表")


class ModalityRunResponse(BaseModel):
    """单模态分析（如 OCR）执行接口的响应模型。"""

    video_id: str
    modality: EventModality
    event_count: int = Field(ge=0, description="本次产出的事件数量")
