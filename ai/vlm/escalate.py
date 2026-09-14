"""escalation 判定与多模态风险融合。

32B 复审本期未部署：判定 needs_escalation 后若调用 32B 得到 501，
管线保留 8B 结果并标注待复审，不得中断主链路。
"""

from dataclasses import dataclass

from ai.vlm.schemas import (
    RISK_CATEGORIES,
    SEVERITY_LEVELS,
    VlmReviewResult,
)

# 低于该默认阈值视为低置信（可被配置覆盖）
DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.6

# OCR/ASR 文本中用于检测模态冲突的关键词（召回优先，宁报勿漏）
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "porn": ("色情", "裸体", "性行为", "裸露"),
    "violence": ("刺向", "去死", "杀害", "持刀", "暴力"),
    "blood": ("鲜血", "流血", "血腥"),
    "politics": ("颠覆政权", "反政府"),
    "illegal": ("毒品交易", "违禁品"),
    "abuse": ("辱骂", "仇恨歧视"),
    "privacy": ("身份证号", "银行密码"),
}

SEVERITY_RANK = {level: index for index, level in enumerate(SEVERITY_LEVELS)}

SUGGESTION_BY_SEVERITY = {
    "none": "通过",
    "low": "复核",
    "medium": "复核",
    "high": "拦截",
}


@dataclass(frozen=True)
class EscalationDecision:
    """是否需要升级到复审模型，以及触发原因。"""

    needs_escalation: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FusedVerdict:
    """融合 VLM 主审与 OCR/ASR 文本信号后的整体结论。"""

    risk: bool
    category: str
    severity: str
    confidence: float
    suggestion: str
    reason: str
    needs_escalation: bool
    escalation_status: str


def detect_text_categories(text: str) -> set[str]:
    """从 OCR/ASR 拼接文本中命中的风险类目集合。"""
    if not text or not text.strip():
        return set()
    found: set[str] = set()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            found.add(category)
    return found


def has_modality_conflict(
    result: VlmReviewResult, ocr_text: str = "", asr_text: str = ""
) -> bool:
    """VLM 报无风险但 OCR/ASR 文本含明确风险信号时视为模态冲突。"""
    found = detect_text_categories(f"{ocr_text}\n{asr_text}")
    if not found:
        return False
    return not result.risk


def should_escalate(
    result: VlmReviewResult | None,
    *,
    json_invalid: bool = False,
    ocr_text: str = "",
    asr_text: str = "",
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
) -> EscalationDecision:
    """判定 8B 主审结果是否需要 escalation。

    触发条件：JSON 不合格、低置信、高风险、模态冲突。
    """
    reasons: list[str] = []
    if json_invalid or result is None:
        reasons.append("json_invalid")
    if result is not None:
        if result.confidence < low_confidence_threshold:
            reasons.append("low_confidence")
        if result.risk and result.severity == "high":
            reasons.append("high_risk")
        if has_modality_conflict(result, ocr_text, asr_text):
            reasons.append("modality_conflict")
    # 去重并保持稳定顺序
    ordered = tuple(dict.fromkeys(reasons))
    return EscalationDecision(needs_escalation=bool(ordered), reasons=ordered)


def _pick_top_result(results: list[VlmReviewResult]) -> VlmReviewResult | None:
    risky = [item for item in results if item.risk]
    if not risky:
        return None
    return max(risky, key=lambda item: (SEVERITY_RANK.get(item.severity, 0), item.confidence))


def _highest_text_category(text_categories: set[str]) -> str:
    for category in RISK_CATEGORIES:
        if category != "none" and category in text_categories:
            return category
    return "other"


def _with_pending_prefix(suggestion: str, needs_escalation: bool) -> str:
    if not needs_escalation:
        return suggestion
    if suggestion.startswith("待复审"):
        return suggestion
    if not suggestion:
        return "待复审"
    return f"待复审；{suggestion}"


def fuse_overall(
    results: list[VlmReviewResult],
    *,
    needs_escalation: bool,
    escalation_status: str,
    ocr_text: str = "",
    asr_text: str = "",
) -> FusedVerdict:
    """把多段 VLM 结果与 OCR/ASR 文本信号融合成整体结论。"""
    top = _pick_top_result(results)
    text_categories = detect_text_categories(f"{ocr_text}\n{asr_text}")
    pending = needs_escalation or escalation_status == "pending_review"
    status = escalation_status
    if pending and status == "not_needed":
        status = "pending_review"

    if top is None:
        if text_categories:
            category = _highest_text_category(text_categories)
            return FusedVerdict(
                risk=True,
                category=category,
                severity="low",
                confidence=0.0,
                suggestion="待复审",
                reason="OCR/ASR 文本含风险信号但 VLM 未报风险，标记待复审",
                needs_escalation=True,
                escalation_status="pending_review",
            )
        return FusedVerdict(
            risk=False,
            category="none",
            severity="none",
            confidence=max((item.confidence for item in results), default=0.0),
            suggestion=_with_pending_prefix("通过", pending),
            reason="未发现风险" if not pending else "主审未发现风险，但存在待复审标记",
            needs_escalation=pending,
            escalation_status=status,
        )

    suggestion = top.suggestion.strip() or SUGGESTION_BY_SEVERITY.get(top.severity, "复核")
    return FusedVerdict(
        risk=True,
        category=top.category,
        severity=top.severity,
        confidence=top.confidence,
        suggestion=_with_pending_prefix(suggestion, pending),
        reason=top.reason,
        needs_escalation=pending,
        escalation_status=status,
    )
