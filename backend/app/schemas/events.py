"""多模态统一时间线事件 schema（预留，仅定义数据结构）。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class TimelineEvent(BaseModel):
    """统一时间线事件：未来 OCR/ASR/视觉/VLM 各模态的产出均归一到该结构。"""

    id: str = Field(description="事件唯一标识")
    video_id: str = Field(description="所属视频标识")
    modality: Literal["ocr", "asr", "vision", "vlm"] = Field(description="事件来源模态")
    start_ms: int = Field(ge=0, description="事件起始时间（毫秒）")
    end_ms: int = Field(ge=0, description="事件结束时间（毫秒）")
    content: str = Field(description="事件内容（文本或描述）")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度")
    metadata: dict[str, Any] = Field(default_factory=dict, description="模态特定的附加信息")


# 事件来源模态：与 TimelineEvent.modality 取值一致
EventModality = Literal["ocr", "asr", "vision", "vlm"]


class ModalityRunResponse(BaseModel):
    """单模态分析（如 OCR）执行接口的响应模型。"""

    video_id: str
    modality: EventModality
    event_count: int = Field(ge=0, description="本次产出的事件数量")
