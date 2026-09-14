"""modal/asr_logic.py 纯逻辑单测：不导入 Modal SDK，不访问云端。"""

from __future__ import annotations

import importlib.util
import io
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

_LOGIC_PATH = Path(__file__).resolve().parents[1] / "modal" / "asr_logic.py"


def _load_logic():
    spec = importlib.util.spec_from_file_location("asr_logic", _LOGIC_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["asr_logic"] = module
    spec.loader.exec_module(module)
    return module


logic = _load_logic()


def _pcm16_wav(nframes: int, rate: int = 16000, channels: int = 1, sampwidth: int = 2) -> bytes:
    frames = b"\x00\x00" * nframes * channels if sampwidth == 2 else b"\x00" * nframes * channels
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buf.getvalue()


def test_validate_model_id_accepts_short_and_repo() -> None:
    assert logic.validate_model_id("Qwen3-ASR-1.7B") == "Qwen3-ASR-1.7B"
    assert logic.validate_model_id("Qwen/Qwen3-ASR-1.7B") == "Qwen/Qwen3-ASR-1.7B"


@pytest.mark.parametrize("value", ["", "   ", None, 123, "whisper-large"])
def test_validate_model_id_rejects(value: object) -> None:
    with pytest.raises(logic.AsrRequestError, match="model|模型"):
        logic.validate_model_id(value)


def test_validate_wav_bytes_accepts_16k_mono_pcm() -> None:
    data = _pcm16_wav(16000)
    info = logic.validate_wav_bytes(data)
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.sampwidth == 2
    assert info.nframes == 16000
    assert info.duration_ms == 1000
    assert len(info.pcm) == 32000


def test_validate_wav_bytes_rejects_empty() -> None:
    with pytest.raises(logic.AsrRequestError, match="空") as exc:
        logic.validate_wav_bytes(b"")
    assert exc.value.status_code == 400


def test_validate_wav_bytes_rejects_not_wav() -> None:
    with pytest.raises(logic.AsrRequestError, match="wav"):
        logic.validate_wav_bytes(b"this is not a wav file")


def test_validate_wav_bytes_rejects_stereo() -> None:
    with pytest.raises(logic.AsrRequestError, match="声道"):
        logic.validate_wav_bytes(_pcm16_wav(1600, channels=2))


def test_validate_wav_bytes_rejects_8khz() -> None:
    with pytest.raises(logic.AsrRequestError, match="采样率"):
        logic.validate_wav_bytes(_pcm16_wav(8000, rate=8000))


def test_validate_wav_bytes_rejects_8bit() -> None:
    with pytest.raises(logic.AsrRequestError, match="位宽"):
        logic.validate_wav_bytes(_pcm16_wav(1600, sampwidth=1))


def test_validate_wav_bytes_rejects_too_large() -> None:
    payload = b"RIFF" + b"\x00" * (logic.MAX_AUDIO_BYTES + 8)
    with pytest.raises(logic.AsrRequestError, match="过大") as exc:
        logic.validate_wav_bytes(payload)
    assert exc.value.status_code == 413


def test_validate_wav_bytes_rejects_too_long() -> None:
    nframes = logic.MAX_DURATION_MS * 16 + 16000
    data = _pcm16_wav(nframes)
    with pytest.raises(logic.AsrRequestError, match="过长") as exc:
        logic.validate_wav_bytes(data)
    assert exc.value.status_code == 413


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Chinese", "zh"),
        ("zh-CN", "zh"),
        ("English", "en"),
        ("Japanese", "ja"),
        ("Cantonese", "yue"),
        ("fr", "fr"),
        (None, "zh"),
        ("", "zh"),
        ("UnknownLang", "zh"),
    ],
)
def test_map_language(raw: object, expected: str) -> None:
    assert logic.map_language(raw) == expected


def test_extract_and_merge_sentence_spans() -> None:
    stamps = [
        SimpleNamespace(text="你", start_time=0.0, end_time=0.12),
        SimpleNamespace(text="好", start_time=0.12, end_time=0.24),
        SimpleNamespace(text="。", start_time=0.24, end_time=0.30),
        SimpleNamespace(text="世", start_time=0.80, end_time=0.92),
        SimpleNamespace(text="界", start_time=0.92, end_time=1.05),
    ]
    spans = logic.merge_spans(logic.extract_align_spans(stamps, duration_ms=2000))
    assert [span.text for span in spans] == ["你好。", "世界"]
    segments = logic.spans_to_segments(spans, duration_ms=2000)
    assert segments[0] == {
        "text": "你好。",
        "start_ms": 0,
        "end_ms": 300,
        "confidence": 1.0,
    }
    assert segments[1]["start_ms"] == 800
    assert segments[1]["end_ms"] == 1050
    assert isinstance(segments[0]["start_ms"], int)
    assert isinstance(segments[0]["end_ms"], int)


def test_extract_align_spans_accepts_dict_and_ms_heuristic() -> None:
    stamps = [{"text": "hi", "start_time": 0, "end_time": 1200}]
    spans = logic.extract_align_spans(stamps, duration_ms=1500)
    assert len(spans) == 1
    assert spans[0].start_s == 0.0
    assert spans[0].end_s == pytest.approx(1.2)


def test_normalize_empty_result() -> None:
    payload = logic.normalize_asr_output([], duration_ms=1000)
    assert payload == {"segments": [], "language": "zh"}


def test_normalize_no_speech_object() -> None:
    raw = SimpleNamespace(language="Chinese", text="  ", time_stamps=[])
    payload = logic.normalize_asr_output(raw, duration_ms=500)
    assert payload["segments"] == []
    assert payload["language"] == "zh"


def test_normalize_text_without_timestamps_fallback() -> None:
    raw = [SimpleNamespace(language="English", text="hello", time_stamps=None)]
    payload = logic.normalize_asr_output(raw, duration_ms=1234)
    assert payload["language"] == "en"
    assert payload["segments"] == [
        {"text": "hello", "start_ms": 0, "end_ms": 1234, "confidence": 1.0}
    ]


def test_normalize_clamps_to_duration() -> None:
    raw = SimpleNamespace(
        language="zh",
        text="a",
        time_stamps=[SimpleNamespace(text="a", start_time=0.0, end_time=5.0)],
    )
    payload = logic.normalize_asr_output(raw, duration_ms=1000)
    assert payload["segments"][0]["end_ms"] == 1000


def test_is_no_speech_error() -> None:
    assert logic.is_no_speech_error(RuntimeError("no speech detected"))
    assert not logic.is_no_speech_error(RuntimeError("CUDA OOM"))


def test_pcm16_header_is_riff() -> None:
    data = _pcm16_wav(16)
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
