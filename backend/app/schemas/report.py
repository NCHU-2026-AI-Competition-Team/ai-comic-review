"""审核报告 schema：供 GET /videos/{video_id}/report 与后续导出使用。"""

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.events import EscalationStatus, TimelineEvent
from app.schemas.video import SamplingMode, VideoMetadata, VideoStatus


class ModalityRunStatus(BaseModel):
    """单个模态的运行状态。"""

    ran: bool = Field(description="是否已落盘该模态结果")
    event_count: int = Field(default=0, ge=0, description="事件数量")


class OverallConclusion(BaseModel):
    """融合后的整体结论。"""

    risk: bool
    category: str
    severity: str
    confidence: float = Field(ge=0.0, le=1.0)
    suggestion: str
    reason: str
    needs_escalation: bool
    escalation_status: EscalationStatus


class ReviewReport(BaseModel):
    """结构化审核报告。"""

    video_id: str
    filename: str
    status: VideoStatus
    sampling: SamplingMode
    metadata: Optional[VideoMetadata] = None
    modalities: dict[str, ModalityRunStatus] = Field(description="ocr / asr / vlm 运行状态")
    risk_events: list[TimelineEvent] = Field(description="风险事件，按 severity 降序")
    overall: OverallConclusion
