"""云端 ASR 契约纯逻辑：wav 校验、语言映射、ForcedAligner 时间戳转毫秒分段。

本模块不依赖 Modal / torch / qwen-asr，便于本地单测。
目录名 modal/ 仅表示部署入口，不是 Python 包（避免与 Modal SDK 同名冲突）。
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# 与 backend/app/services/audio.py 及 Provider 契约一致
REQUIRED_SAMPLE_RATE = 16000
REQUIRED_CHANNELS = 1
REQUIRED_SAMPWIDTH = 2  # PCM s16le
DEFAULT_LANGUAGE = "zh"
DEFAULT_CONFIDENCE = 1.0
# 单请求上限：16kHz 单声道 16bit 约 1.92MB/分钟，50MB ~ 26 分钟
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_DURATION_MS = 15 * 60 * 1000
# 句间停顿超过该阈值则切段（秒）
GAP_SPLIT_SECONDS = 0.4
SENTENCE_ENDINGS = frozenset("。！？!?.;；…")

# 本地 Provider 发送的模型标识，同时接受 HuggingFace repo id
ACCEPTED_MODEL_IDS = frozenset({"Qwen3-ASR-1.7B", "Qwen/Qwen3-ASR-1.7B"})

# Qwen3-ASR 官方语言名 → ISO 639-1（粤语用 yue）
LANGUAGE_TO_ISO = {
    "chinese": "zh",
    "mandarin": "zh",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "cantonese": "yue",
    "yue": "yue",
    "english": "en",
    "en": "en",
    "japanese": "ja",
    "ja": "ja",
    "korean": "ko",
    "ko": "ko",
    "arabic": "ar",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "portuguese": "pt",
    "indonesian": "id",
    "italian": "it",
    "russian": "ru",
    "thai": "th",
    "vietnamese": "vi",
    "turkish": "tr",
    "hindi": "hi",
    "malay": "ms",
    "dutch": "nl",
    "swedish": "sv",
    "danish": "da",
    "finnish": "fi",
    "norwegian": "no",
    "polish": "pl",
    "czech": "cs",
    "filipino": "tl",
    "tagalog": "tl",
    "persian": "fa",
    "farsi": "fa",
    "greek": "el",
    "hungarian": "hu",
    "macedonian": "mk",
}


class AsrRequestError(ValueError):
    """畸形输入：对应 HTTP 4xx。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class WavInfo:
    """已通过契约校验的 PCM wav 元数据。"""

    sample_rate: int
    channels: int
    sampwidth: int
    nframes: int
    duration_ms: int
    pcm: bytes


@dataclass(frozen=True)
class AlignSpan:
    """ForcedAligner 单段：文本 + 秒级起止。"""

    text: str
    start_s: float
    end_s: float


def validate_model_id(model: object) -> str:
    """校验表单字段 model；非法时抛 400。"""
    if not isinstance(model, str) or not model.strip():
        raise AsrRequestError("缺少或非法的表单字段 model，期望 Qwen3-ASR-1.7B")
    model_id = model.strip()
    if model_id not in ACCEPTED_MODEL_IDS:
        raise AsrRequestError(
            f"不支持的模型标识 {model_id!r}，期望 Qwen3-ASR-1.7B"
        )
    return model_id


def validate_wav_bytes(data: bytes) -> WavInfo:
    """校验 16kHz 单声道 PCM16 wav；畸形输入抛 AsrRequestError。"""
    if not isinstance(data, (bytes, bytearray)):
        raise AsrRequestError("音频内容非法：期望二进制 wav")
    payload = bytes(data)
    if not payload:
        raise AsrRequestError("音频文件为空")
    if len(payload) > MAX_AUDIO_BYTES:
        raise AsrRequestError(
            f"音频过大：{len(payload)} 字节，上限 {MAX_AUDIO_BYTES} 字节",
            status_code=413,
        )
    try:
        with wave.open(io.BytesIO(payload), "rb") as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            sampwidth = wav.getsampwidth()
            nframes = wav.getnframes()
            comptype = (wav.getcomptype() or "NONE").upper()
            pcm = wav.readframes(nframes)
    except wave.Error as exc:
        raise AsrRequestError(f"不是合法的 wav 音频：{exc}") from exc
    except EOFError as exc:
        raise AsrRequestError("wav 文件截断或不完整") from exc

    if comptype not in {"NONE", "NOT COMPRESSED"}:
        raise AsrRequestError(f"仅支持 PCM wav，实际压缩类型为 {comptype}")
    if channels != REQUIRED_CHANNELS:
        raise AsrRequestError(
            f"音频声道数非法：{channels}，期望 {REQUIRED_CHANNELS}（单声道）"
        )
    if rate != REQUIRED_SAMPLE_RATE:
        raise AsrRequestError(
            f"音频采样率非法：{rate}Hz，期望 {REQUIRED_SAMPLE_RATE}Hz"
        )
    if sampwidth != REQUIRED_SAMPWIDTH:
        raise AsrRequestError(
            f"音频采样位宽非法：{sampwidth * 8}bit，期望 PCM 16bit"
        )
    if nframes <= 0 or not pcm:
        raise AsrRequestError("音频无采样帧")

    duration_ms = int(round(nframes * 1000.0 / rate))
    if duration_ms > MAX_DURATION_MS:
        raise AsrRequestError(
            f"音频过长：{duration_ms}ms，上限 {MAX_DURATION_MS}ms",
            status_code=413,
        )
    return WavInfo(
        sample_rate=rate,
        channels=channels,
        sampwidth=sampwidth,
        nframes=nframes,
        duration_ms=duration_ms,
        pcm=pcm,
    )


def map_language(raw: object) -> str:
    """把 Qwen3-ASR 语言名映射为契约使用的短码，缺省 zh。"""
    if raw is None:
        return DEFAULT_LANGUAGE
    text = str(raw).strip()
    if not text:
        return DEFAULT_LANGUAGE
    key = text.lower().replace("_", "-")
    if key in LANGUAGE_TO_ISO:
        return LANGUAGE_TO_ISO[key]
    if 2 <= len(key) <= 3 and key.isalpha():
        return key
    return DEFAULT_LANGUAGE


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AsrRequestError(f"对齐时间戳 {field} 类型非法：{type(value).__name__}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise AsrRequestError(f"对齐时间戳 {field} 不是有限数值：{value}")
    return number


def _to_seconds(value: float, duration_s: float) -> float:
    """官方 ForcedAlignItem 为秒；仅当数值像毫秒（≥100 且远超音频秒数）才换算。"""
    if value >= 100.0 and (duration_s <= 0 or value > duration_s * 2.0 + 1.0):
        return value / 1000.0
    return value


def _attr_or_key(item: object, *names: str) -> object:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
        return None
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return None


def extract_align_spans(time_stamps: object, duration_ms: int) -> list[AlignSpan]:
    """从 ForcedAligner 输出提取秒级片段；无法识别的条目跳过。"""
    if time_stamps is None:
        return []
    if isinstance(time_stamps, (str, bytes, bytearray)):
        return []
    if not isinstance(time_stamps, Sequence) or isinstance(time_stamps, (str, bytes)):
        return []

    duration_s = max(duration_ms, 0) / 1000.0
    spans: list[AlignSpan] = []
    for item in time_stamps:
        if item is None:
            continue
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, Mapping)):
            # 可能是 batch 嵌套
            spans.extend(extract_align_spans(item, duration_ms))
            continue
        text_raw = _attr_or_key(item, "text", "token")
        start_raw = _attr_or_key(item, "start_time", "start", "start_s")
        end_raw = _attr_or_key(item, "end_time", "end", "end_s")
        if text_raw is None or start_raw is None or end_raw is None:
            continue
        text = str(text_raw)
        if not text:
            continue
        start_s = _to_seconds(_finite_float(start_raw, "start"), duration_s)
        end_s = _to_seconds(_finite_float(end_raw, "end"), duration_s)
        if end_s < start_s:
            start_s, end_s = end_s, start_s
        start_s = max(0.0, start_s)
        if duration_s > 0:
            end_s = min(end_s, duration_s)
            start_s = min(start_s, end_s)
        spans.append(AlignSpan(text=text, start_s=start_s, end_s=end_s))
    spans.sort(key=lambda span: (span.start_s, span.end_s))
    return spans


def merge_spans(
    spans: Sequence[AlignSpan],
    gap_split_seconds: float = GAP_SPLIT_SECONDS,
) -> list[AlignSpan]:
    """按标点与停顿把字/词级对齐合并为句子级分段。"""
    if not spans:
        return []
    merged: list[AlignSpan] = []
    current_text = spans[0].text
    current_start = spans[0].start_s
    current_end = spans[0].end_s
    for span in spans[1:]:
        gap = span.start_s - current_end
        should_split = bool(current_text) and (
            current_text[-1] in SENTENCE_ENDINGS or gap > gap_split_seconds
        )
        if should_split:
            merged.append(
                AlignSpan(text=current_text, start_s=current_start, end_s=current_end)
            )
            current_text = span.text
            current_start = span.start_s
            current_end = span.end_s
        else:
            current_text += span.text
            current_end = max(current_end, span.end_s)
    merged.append(AlignSpan(text=current_text, start_s=current_start, end_s=current_end))
    return merged


def spans_to_segments(
    spans: Iterable[AlignSpan],
    duration_ms: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> list[dict[str, Any]]:
    """秒级片段转为契约分段：start_ms/end_ms 为 int，confidence 为 0~1。"""
    if confidence < 0.0:
        confidence = 0.0
    if confidence > 1.0:
        confidence = 1.0
    segments: list[dict[str, Any]] = []
    for span in spans:
        text = span.text.strip()
        if not text:
            continue
        start_ms = int(round(span.start_s * 1000.0))
        end_ms = int(round(span.end_s * 1000.0))
        start_ms = max(0, start_ms)
        end_ms = max(start_ms, end_ms)
        if duration_ms > 0:
            end_ms = min(end_ms, duration_ms)
            start_ms = min(start_ms, end_ms)
        segments.append(
            {
                "text": text,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "confidence": float(confidence),
            }
        )
    return segments


def _first_result_item(raw: object) -> object:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray, Mapping)):
        return raw[0] if raw else None
    return raw


def normalize_asr_output(raw: object, duration_ms: int) -> dict[str, Any]:
    """把 qwen-asr transcribe 返回值规范为 Provider 契约 JSON。

    ForcedAligner 不提供置信度，有对齐文本时 confidence 固定为 1.0。
    无语音 / 空转写返回空 segments，language 仍给 zh（或检测到的语言）。
    """
    item = _first_result_item(raw)
    if item is None:
        return {"segments": [], "language": DEFAULT_LANGUAGE}

    language = map_language(_attr_or_key(item, "language", "lang"))
    text_raw = _attr_or_key(item, "text", "transcript")
    text = str(text_raw).strip() if text_raw is not None else ""
    time_stamps = _attr_or_key(item, "time_stamps", "timestamps", "alignment")
    spans = merge_spans(extract_align_spans(time_stamps, duration_ms))
    segments = spans_to_segments(spans, duration_ms)

    if not segments and text:
        end_ms = max(int(duration_ms), 0)
        segments = [
            {
                "text": text,
                "start_ms": 0,
                "end_ms": end_ms,
                "confidence": DEFAULT_CONFIDENCE,
            }
        ]
    return {"segments": segments, "language": language}


def is_no_speech_error(exc: BaseException) -> bool:
    """判断是否为无语音类推理错误，可降级为空 segments。"""
    message = str(exc).lower()
    needles = (
        "no speech",
        "no_speech",
        "empty transcription",
        "empty audio",
        "silence",
        "无语音",
        "空音频",
    )
    return any(needle in message for needle in needles)
