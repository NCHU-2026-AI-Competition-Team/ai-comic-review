"""人工复核决策 schema：供 POST /videos/{video_id}/verdict 与报告内嵌字段使用。"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# 复核决定：通过 / 驳回 / 误报；与前端审核工作台按钮取值对齐
VerdictDecision = Literal["approve", "reject", "false_positive"]


class VerdictRequest(BaseModel):
    """人工复核提交请求。"""

    decision: VerdictDecision = Field(description="复核决定：approve / reject / false_positive")
    note: str = Field(default="", description="复核备注，可为空串")
    event_id: Optional[str] = Field(
        default=None, description="针对单条事件的标识；null 表示针对整体结论"
    )


class HumanVerdict(BaseModel):
    """人工复核结论：接口成功响应与报告内嵌对象（不含内部 updated_at）。"""

    video_id: str
    decision: VerdictDecision
    note: str
    event_id: Optional[str] = None
    created_at: datetime = Field(description="首次提交时间，ISO8601")


class StoredVerdict(HumanVerdict):
    """落盘到 storage/outputs/{video_id}/verdict.json 的完整记录。"""

    updated_at: datetime = Field(description="最近一次覆盖写入时间")
