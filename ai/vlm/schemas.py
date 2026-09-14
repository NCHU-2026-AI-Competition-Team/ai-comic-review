"""VLM 主审严格 JSON 响应模型与解析。

字段、枚举与归一化规则与云端契约（modal/vlm/review_contract.py）对齐，
但本模块不 import modal 包，避免本地 modal/ 目录遮蔽 Modal SDK。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai.vlm.base import VlmResponseFormatError

# 风险类目 / 严重级别：与云端契约保持一致（tests 覆盖对照）
RISK_CATEGORIES = (
    "none",
    "porn",
    "violence",
    "blood",
    "politics",
    "illegal",
    "abuse",
    "privacy",
    "other",
)

SEVERITY_LEVELS = ("none", "low", "medium", "high")

SAFE_CATEGORY = "none"
SAFE_SEVERITY = "none"

REVIEW_RESULT_FIELDS = (
    "risk",
    "category",
    "severity",
    "confidence",
    "start_ms",
    "end_ms",
    "evidence",
    "reason",
    "suggestion",
)

# 与云端 TEXT_FORM_FIELDS 单字段上限对齐，管线组装 OCR/ASR 文本时截断
MAX_TEXT_FIELD_CHARS = 20_000

RiskCategory = Literal[
    "none", "porn", "violence", "blood", "politics", "illegal", "abuse", "privacy", "other"
]
SeverityLevel = Literal["none", "low", "medium", "high"]
EscalationStatus = Literal["not_needed", "pending_review", "completed"]


class VlmReviewResult(BaseModel):
    """主审 /review 的严格 JSON 响应。"""

    model_config = ConfigDict(extra="forbid")

    risk: bool = Field(description="是否存在风险")
    category: RiskCategory = Field(description="风险类目；无风险时固定为 none")
    severity: SeverityLevel = Field(description="严重级别；无风险时固定为 none")
    confidence: float = Field(ge=0.0, le=1.0, description="判定置信度，0~1")
    start_ms: int = Field(ge=0, description="风险片段起始毫秒；无风险时为 0")
    end_ms: int = Field(ge=0, description="风险片段结束毫秒；无风险时为 0")
    evidence: str = Field(description="命中的画面/文字证据描述")
    reason: str = Field(description="判定理由")
    suggestion: str = Field(description="处置建议")

    @model_validator(mode="after")
    def _check_consistency(self) -> "VlmReviewResult":
        if self.end_ms < self.start_ms:
            raise ValueError(
                f"时间区间非法：start_ms={self.start_ms} > end_ms={self.end_ms}"
            )
        if not self.risk:
            if (
                self.category != SAFE_CATEGORY
                or self.severity != SAFE_SEVERITY
                or self.start_ms != 0
                or self.end_ms != 0
            ):
                raise ValueError("risk=false 时 category/severity 必须为 none，且起止毫秒必须为 0")
        elif self.category == SAFE_CATEGORY or self.severity == SAFE_SEVERITY:
            raise ValueError(
                f"risk=true 时 category/severity 不得为空值 none，"
                f"实际 category={self.category!r} severity={self.severity!r}"
            )
        return self


def _require_str(data: dict[str, Any], field: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise VlmResponseFormatError(
            f"VLM 返回 {field} 字段类型非法：期望 str，实际 {type(value).__name__}"
        )
    return value


def parse_review_response(data: Any) -> VlmReviewResult:
    """把 /review 的 JSON 返回解析为 VlmReviewResult，结构不符时抛 VlmResponseFormatError。"""
    if not isinstance(data, dict):
        raise VlmResponseFormatError(
            f"VLM 返回结构非法：期望 JSON 对象，实际类型为 {type(data).__name__}"
        )
    missing = [field for field in REVIEW_RESULT_FIELDS if field not in data]
    if missing:
        raise VlmResponseFormatError(
            f"VLM 返回缺少字段 {missing}，期望字段为 {REVIEW_RESULT_FIELDS}"
        )
    extra = [key for key in data if key not in REVIEW_RESULT_FIELDS]
    if extra:
        raise VlmResponseFormatError(f"VLM 返回含未知字段 {extra}")

    risk = data["risk"]
    if not isinstance(risk, bool):
        raise VlmResponseFormatError(
            f"VLM 返回 risk 字段类型非法：期望 bool，实际 {type(risk).__name__}"
        )

    category = _require_str(data, "category")
    if category not in RISK_CATEGORIES:
        raise VlmResponseFormatError(
            f"VLM 返回 category 取值非法：{category!r}，期望 {RISK_CATEGORIES}"
        )
    severity = _require_str(data, "severity")
    if severity not in SEVERITY_LEVELS:
        raise VlmResponseFormatError(
            f"VLM 返回 severity 取值非法：{severity!r}，期望 {SEVERITY_LEVELS}"
        )

    confidence = data["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise VlmResponseFormatError(
            f"VLM 返回 confidence 字段类型非法：期望数值，实际 {type(confidence).__name__}"
        )
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise VlmResponseFormatError(f"VLM 返回 confidence 越界：{confidence}，期望 0~1")

    time_range: dict[str, int] = {}
    for field in ("start_ms", "end_ms"):
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VlmResponseFormatError(
                f"VLM 返回 {field} 必须是非负整数毫秒，实际为 {value!r}"
            )
        time_range[field] = value
    if time_range["end_ms"] < time_range["start_ms"]:
        raise VlmResponseFormatError(
            f"VLM 返回时间区间非法：start_ms={time_range['start_ms']} end_ms={time_range['end_ms']}"
        )
    if risk and (category == SAFE_CATEGORY or severity == SAFE_SEVERITY):
        raise VlmResponseFormatError(
            f"VLM 返回 risk=true 时 category/severity 不得为空值 none，"
            f"实际 category={category!r} severity={severity!r}"
        )

    evidence = _require_str(data, "evidence")
    reason = _require_str(data, "reason")
    suggestion = _require_str(data, "suggestion")

    if not risk:
        category = SAFE_CATEGORY
        severity = SAFE_SEVERITY
        time_range = {"start_ms": 0, "end_ms": 0}

    try:
        return VlmReviewResult.model_validate(
            {
                "risk": risk,
                "category": category,
                "severity": severity,
                "confidence": confidence,
                "start_ms": time_range["start_ms"],
                "end_ms": time_range["end_ms"],
                "evidence": evidence,
                "reason": reason,
                "suggestion": suggestion,
            }
        )
    except ValidationError as exc:
        raise VlmResponseFormatError(f"VLM 返回未通过模型校验：{exc}") from exc
