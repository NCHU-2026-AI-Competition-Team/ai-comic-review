"""VLM 主审服务的纯逻辑契约模块：模型标识解析、输出 schema、prompt 构建与结果校验。

本模块不依赖 modal / vllm / fastapi，仅使用标准库，便于：
- serve.py（Modal 云端服务）与本仓 tests/ 纯逻辑测试共同复用；
- 契约（输入字段、输出 JSON 结构、枚举取值）在一处定义，不散落。

接口形态对齐 ai/asr/qwen_asr.py 的契约风格：端点与模型名走配置，
服务端对不合规输出抛出明确异常，由 serve.py 翻译成 HTTP 状态码。
"""

import json
import math
from typing import Any

# ---------------------------------------------------------------------------
# 模型标识契约：主审 8B 本期部署；32B 复审（escalation）预留位，本期返回 501
# ---------------------------------------------------------------------------

PRIMARY_MODEL_ID = "qwen3-vl-8b-instruct"
PRIMARY_HF_REPO = "Qwen/Qwen3-VL-8B-Instruct"

ESCALATION_MODEL_ID = "qwen3-vl-32b-instruct"

# 客户端可使用的模型标识别名（统一小写后匹配）
PRIMARY_MODEL_ALIASES = frozenset({"qwen3-vl-8b-instruct", "qwen3-vl-8b", "8b"})
ESCALATION_MODEL_ALIASES = frozenset({"qwen3-vl-32b-instruct", "qwen3-vl-32b", "32b"})

MODEL_ROLE_PRIMARY = "primary"
MODEL_ROLE_ESCALATION = "escalation"

# ---------------------------------------------------------------------------
# 输出 JSON 契约
# ---------------------------------------------------------------------------

# 风险类目枚举：仓内尚无既有定义（ai/risk 为占位），本契约为唯一来源
RISK_CATEGORIES = (
    "none",      # 无风险（约定空值）
    "porn",      # 色情/性暗示
    "violence",  # 暴力
    "blood",     # 血腥
    "politics",  # 涉政敏感
    "illegal",   # 违法违规
    "abuse",     # 辱骂/仇恨/歧视
    "privacy",   # 隐私泄露
    "other",     # 其他风险
)

SEVERITY_LEVELS = ("none", "low", "medium", "high")

# 无风险时的约定空值
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

# vLLM 结构化输出（guided decoding）使用的 JSON Schema，
# 与 REVIEW_RESULT_FIELDS / 枚举常量保持一致（手工同步，测试覆盖）
REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "risk": {"type": "boolean", "description": "是否存在风险"},
        "category": {
            "type": "string",
            "enum": list(RISK_CATEGORIES),
            "description": "风险类目；无风险时固定为 none",
        },
        "severity": {
            "type": "string",
            "enum": list(SEVERITY_LEVELS),
            "description": "严重级别；无风险时固定为 none",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "判定置信度，0~1",
        },
        "start_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "风险片段起始毫秒；无风险时为 0",
        },
        "end_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "风险片段结束毫秒；无风险时为 0",
        },
        "evidence": {"type": "string", "description": "命中的画面/文字证据描述"},
        "reason": {"type": "string", "description": "判定理由"},
        "suggestion": {"type": "string", "description": "处置建议"},
    },
    "required": list(REVIEW_RESULT_FIELDS),
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# 输入约束
# ---------------------------------------------------------------------------

MAX_FRAMES_PER_REQUEST = 8          # 单次请求最大帧数（与 vLLM limit_mm_per_prompt 对齐）
MAX_FRAME_BYTES = 10 * 1024 * 1024  # 单帧最大 10MB
MAX_TEXT_FIELD_CHARS = 20_000       # 单个文本表单字段最大字符数
TEXT_FORM_FIELDS = ("ocr_text", "asr_text", "context", "rules")

# 高分辨率帧必须先降采样再进 vLLM。1760x1184 八帧按 factor=28 约 2.1 万图像 token，
# 远超旧 max_model_len=8192，会让引擎直接抛错变成裸 500。
# 长边 768：1080p/1760x1184 仍能读字幕；640x360 低分辨率帧不会被放大。
FRAME_LONG_EDGE = 768

# Qwen2-VL / Qwen2.5-VL 为 patch=14、merge=2 → 28px 一 token。
# Qwen3-VL 官方为 patch=16、merge=2 → 32px 一 token（更少）。
# 这里用 28 做保守高估，避免预检查低估后仍撞引擎上限。
IMAGE_TOKEN_FACTOR = 28
IMAGE_MIN_PIXELS = 4 * IMAGE_TOKEN_FACTOR * IMAGE_TOKEN_FACTOR
PER_IMAGE_SPECIAL_TOKENS = 4        # vision_start / vision_end 等
CHAT_TEMPLATE_TOKEN_OVERHEAD = 128  # chat 模板与结构化输出包装的保守余量

# 降采样后 16:9 八帧图像 token≈3.2k，方形八帧≈5.8k；再加 prompt 与
# MAX_OUTPUT_TOKENS=1024，方形八帧可能顶满 8192。
# L40S 实测 KV cache 约 16.56GB / 12 万 tokens，16384 约占 14%，
# 显存上仍保守；不继续上调，异常超长 OCR/ASR 由 4xx 拦住。
MAX_MODEL_LEN = 16384
MAX_OUTPUT_TOKENS = 1024


class ReviewContractError(ValueError):
    """契约层异常基类。"""


class UnknownModelError(ReviewContractError):
    """model 表单字段既不是已部署主审模型，也不是预留的 escalation 标识。"""


class ResultValidationError(ReviewContractError):
    """模型输出不满足 JSON 契约（serve.py 据此重试或返回 502）。"""


class InputValidationError(ReviewContractError):
    """请求输入畸形（serve.py 翻译成 4xx）。"""


def resolve_model_role(raw: str | None) -> str:
    """把 model 表单字段解析为模型角色。

    空值视为默认主审；返回 MODEL_ROLE_PRIMARY 或 MODEL_ROLE_ESCALATION；
    无法识别的标识抛 UnknownModelError（serve.py 翻译成 400）。
    """
    value = (raw or "").strip().lower()
    if not value or value in PRIMARY_MODEL_ALIASES:
        return MODEL_ROLE_PRIMARY
    if value in ESCALATION_MODEL_ALIASES:
        return MODEL_ROLE_ESCALATION
    raise UnknownModelError(
        f"无法识别的 model 标识 {raw!r}：本期仅部署 {PRIMARY_MODEL_ID}，"
        f"{ESCALATION_MODEL_ID} 为 escalation 预留（未部署）"
    )


def validate_text_fields(fields: dict[str, str]) -> None:
    """校验文本表单字段长度，超限抛 InputValidationError。"""
    for name in TEXT_FORM_FIELDS:
        value = fields.get(name) or ""
        if len(value) > MAX_TEXT_FIELD_CHARS:
            raise InputValidationError(
                f"表单字段 {name} 超长：{len(value)} 字符，上限 {MAX_TEXT_FIELD_CHARS}"
            )


def parse_frame_timestamps(raw: str, frame_count: int) -> list[int]:
    """解析 frame_timestamps_ms 表单字段（JSON 整数数组，毫秒）。

    空值时按 0 起等差 500ms 生成默认时间轴；长度与帧数不一致或
    元素非法时抛 InputValidationError。
    """
    raw = (raw or "").strip()
    if not raw:
        return [index * 500 for index in range(frame_count)]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputValidationError(f"frame_timestamps_ms 不是合法 JSON：{exc}") from exc
    if not isinstance(data, list) or len(data) != frame_count:
        raise InputValidationError(
            f"frame_timestamps_ms 必须是长度为 {frame_count} 的数组，实际为 {type(data).__name__}"
        )
    timestamps: list[int] = []
    for index, item in enumerate(data):
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise InputValidationError(
                f"frame_timestamps_ms[{index}] 必须是非负整数毫秒，实际为 {item!r}"
            )
        timestamps.append(item)
    return timestamps


def fit_long_edge(
    width: int,
    height: int,
    long_edge: int = FRAME_LONG_EDGE,
) -> tuple[int, int]:
    """按最长边限制缩放，保持宽高比；不超过上限则原样返回。

    尺寸非法时抛 InputValidationError。结果至少为 1x1。
    """
    if width <= 0 or height <= 0:
        raise InputValidationError(f"非法帧尺寸 {width}x{height}")
    if long_edge <= 0:
        raise InputValidationError(f"非法长边上限 {long_edge}")
    current_long = max(width, height)
    if current_long <= long_edge:
        return width, height
    scale = long_edge / current_long
    return max(1, round(width * scale)), max(1, round(height * scale))


def _round_by_factor(number: int, factor: int) -> int:
    return round(number / factor) * factor


def aligned_vision_size(
    width: int,
    height: int,
    factor: int = IMAGE_TOKEN_FACTOR,
) -> tuple[int, int]:
    """把宽高对齐到 factor 倍数，模拟 Qwen-VL smart_resize（不含 max_pixels 再压缩）。"""
    if width <= 0 or height <= 0:
        raise InputValidationError(f"非法帧尺寸 {width}x{height}")
    h_bar = max(factor, _round_by_factor(height, factor))
    w_bar = max(factor, _round_by_factor(width, factor))
    if h_bar * w_bar < IMAGE_MIN_PIXELS:
        beta = math.sqrt(IMAGE_MIN_PIXELS / max(1, height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return w_bar, h_bar


def estimate_image_tokens(width: int, height: int) -> int:
    """估算单帧图像 token（空间网格，不含 special token）。"""
    aligned_w, aligned_h = aligned_vision_size(width, height)
    return (aligned_w // IMAGE_TOKEN_FACTOR) * (aligned_h // IMAGE_TOKEN_FACTOR)


def estimate_review_prompt_tokens(
    image_sizes: list[tuple[int, int]],
    prompt_text: str,
) -> int:
    """估算 /review 一次请求的输入 token（图像 + 文本 + 模板开销）。

    文本按 1 token/字符计：中文约 1:1，英文偏高估，用作硬上限预检查。
    """
    image_tokens = sum(estimate_image_tokens(width, height) for width, height in image_sizes)
    special = PER_IMAGE_SPECIAL_TOKENS * len(image_sizes)
    text_tokens = max(1, len(prompt_text))
    return image_tokens + special + text_tokens + CHAT_TEMPLATE_TOKEN_OVERHEAD


def ensure_review_fits_model_len(
    image_sizes: list[tuple[int, int]],
    prompt_text: str,
    *,
    max_model_len: int = MAX_MODEL_LEN,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
) -> int:
    """输入 token + 输出预留超过 max_model_len 时抛 InputValidationError。

    返回估算的输入 token 数，供日志与测试断言。
    """
    input_tokens = estimate_review_prompt_tokens(image_sizes, prompt_text)
    needed = input_tokens + max_output_tokens
    if needed > max_model_len:
        size_desc = ", ".join(f"{width}x{height}" for width, height in image_sizes) or "（无）"
        image_tokens = sum(
            estimate_image_tokens(width, height) for width, height in image_sizes
        )
        raise InputValidationError(
            f"图像 token 估算超限：{len(image_sizes)} 帧降采样后尺寸 [{size_desc}]，"
            f"图像 token≈{image_tokens}，prompt 输入≈{input_tokens}，"
            f"加输出预留 {max_output_tokens} 共 {needed}，超过 max_model_len={max_model_len}。"
            "请减少帧数、缩短 OCR/ASR 文本，或降低分辨率。"
        )
    return input_tokens


def is_model_len_error(exc: BaseException) -> bool:
    """识别 vLLM 因 prompt/图像 token 超过 max_model_len 抛出的异常。"""
    text = str(exc).lower()
    needles = (
        "max_model_len",
        "maximum model length",
        "longer than the maximum",
        "exceeds the model's context",
        "context length",
    )
    return any(needle in text for needle in needles)


def build_review_prompt(
    frame_timestamps_ms: list[int],
    ocr_text: str = "",
    asr_text: str = "",
    context: str = "",
    rules: str = "",
) -> str:
    """构建主审 prompt：多帧图 + OCR/ASR 文本上下文 + 审核规则。

    审核系统 Recall 优先：明确指示模型对疑似风险倾向上报，由置信度表达把握程度。
    """
    timeline = "、".join(f"第{i + 1}帧(t={ts}ms)" for i, ts in enumerate(frame_timestamps_ms))
    ocr_block = ocr_text.strip() or "（无）"
    asr_block = asr_text.strip() or "（无）"
    context_block = context.strip() or "（无）"
    rules_block = rules.strip() or "按通用内容安全规范审核：色情、暴力、血腥、涉政、违法、辱骂仇恨、隐私泄露。"
    return (
        "你是内容安全审核员，审核一段视频的关键帧与配套文本。宁可多报疑似风险（Recall 优先），"
        "把握不足时通过较低的 confidence 表达，而不是漏报。\n\n"
        f"【帧时间轴】按顺序给出 {len(frame_timestamps_ms)} 帧：{timeline}\n"
        f"【画面文字 OCR】{ocr_block}\n"
        f"【语音转写 ASR】{asr_block}\n"
        f"【上下文】{context_block}\n"
        f"【审核规则】{rules_block}\n\n"
        "只输出一个 JSON 对象，字段：\n"
        '- "risk": 布尔，是否存在风险\n'
        '- "category": 风险类目，取值之一 ' + json.dumps(list(RISK_CATEGORIES), ensure_ascii=False) + "，无风险固定 none\n"
        '- "severity": 严重级别，取值之一 ' + json.dumps(list(SEVERITY_LEVELS), ensure_ascii=False) + "，无风险固定 none\n"
        '- "confidence": 0~1 数值\n'
        '- "start_ms"/"end_ms": 风险片段的起止毫秒整数（参考帧时间轴），无风险时为 0\n'
        '- "evidence": 命中的画面/文字证据（注明出自第几帧或 OCR/ASR 哪段）\n'
        '- "reason": 判定理由\n'
        '- "suggestion": 处置建议（如 通过/复核/拦截）'
    )


def _require_str(data: dict[str, Any], field: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise ResultValidationError(
            f"字段 {field} 类型非法：期望 str，实际 {type(value).__name__}"
        )
    return value


def validate_review_result(data: Any) -> dict[str, Any]:
    """校验并归一化模型输出的审核结果，不合规抛 ResultValidationError。

    归一化规则：risk=false 时强制 category/severity 为约定空值、
    start_ms/end_ms 归零，保证下游消费的一致性。
    """
    if not isinstance(data, dict):
        raise ResultValidationError(
            f"审核结果必须是 JSON 对象，实际为 {type(data).__name__}"
        )
    missing = [field for field in REVIEW_RESULT_FIELDS if field not in data]
    if missing:
        raise ResultValidationError(f"审核结果缺少字段 {missing}")

    risk = data["risk"]
    if not isinstance(risk, bool):
        raise ResultValidationError(
            f"字段 risk 类型非法：期望 bool，实际 {type(risk).__name__}"
        )

    category = _require_str(data, "category")
    if category not in RISK_CATEGORIES:
        raise ResultValidationError(f"字段 category 取值非法：{category!r}，期望 {RISK_CATEGORIES}")
    severity = _require_str(data, "severity")
    if severity not in SEVERITY_LEVELS:
        raise ResultValidationError(f"字段 severity 取值非法：{severity!r}，期望 {SEVERITY_LEVELS}")

    confidence = data["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ResultValidationError(
            f"字段 confidence 类型非法：期望数值，实际 {type(confidence).__name__}"
        )
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ResultValidationError(f"字段 confidence 越界：{confidence}，期望 0~1")

    time_range: dict[str, int] = {}
    for field in ("start_ms", "end_ms"):
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ResultValidationError(
                f"字段 {field} 必须是非负整数毫秒，实际为 {value!r}"
            )
        time_range[field] = value
    if time_range["end_ms"] < time_range["start_ms"]:
        raise ResultValidationError(
            f"时间区间非法：start_ms={time_range['start_ms']} > end_ms={time_range['end_ms']}"
        )

    evidence = _require_str(data, "evidence")
    reason = _require_str(data, "reason")
    suggestion = _require_str(data, "suggestion")

    if not risk:
        # 无风险：归一化为约定空值
        return {
            "risk": False,
            "category": SAFE_CATEGORY,
            "severity": SAFE_SEVERITY,
            "confidence": confidence,
            "start_ms": 0,
            "end_ms": 0,
            "evidence": evidence,
            "reason": reason,
            "suggestion": suggestion,
        }
    if category == SAFE_CATEGORY or severity == SAFE_SEVERITY:
        raise ResultValidationError(
            f"risk=true 时 category/severity 不得为空值 none，实际 category={category!r} severity={severity!r}"
        )
    return {
        "risk": True,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "start_ms": time_range["start_ms"],
        "end_ms": time_range["end_ms"],
        "evidence": evidence,
        "reason": reason,
        "suggestion": suggestion,
    }
