"""ASR 流水线的纯逻辑单元测试。

不依赖真实 Modal 服务与 ffmpeg：通过注入 fake Provider 并 monkeypatch
ensure_audio 隔离引擎与音频提取，覆盖分段聚合、空文本跳过、
置信度截断与防回归（业务层不得 import 具体实现模块）。
"""

import json
from pathlib import Path

import pytest

from ai.asr.base import AsrResult, AsrSegment
from app.core.config import get_settings
from app.services import asr_pipeline
from app.services.asr_pipeline import run_asr, segments_to_events

VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例使用独立的临时存储目录，并桩掉音频提取。"""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    fake_audio = tmp_path / "audio.wav"
    fake_audio.write_bytes(b"RIFF" + b"\x00" * 40)
    monkeypatch.setattr(asr_pipeline, "ensure_audio", lambda video_id: fake_audio)
    yield
    get_settings.cache_clear()


def _segments() -> list[AsrSegment]:
    return [
        AsrSegment(text="第一句台词", start_ms=0, end_ms=1200, confidence=0.95),
        AsrSegment(text="第二句台词", start_ms=1500, end_ms=3000, confidence=0.85),
    ]


class FakeProvider:
    """返回固定识别结果的桩引擎。"""

    def __init__(self, result: AsrResult | None = None) -> None:
        self._result = result or AsrResult(segments=_segments(), language="zh")

    def transcribe(self, audio_path: Path) -> AsrResult:
        return self._result


def test_segments_to_events_aggregation() -> None:
    """分段聚合：时间戳取分段区间，语言与序号入 metadata。"""
    events = segments_to_events(VIDEO_ID, _segments(), "zh")

    assert len(events) == 2
    first = events[0]
    assert first.id == "asr-000000"
    assert first.video_id == VIDEO_ID
    assert first.modality == "asr"
    assert first.start_ms == 0 and first.end_ms == 1200
    assert first.content == "第一句台词"
    assert first.confidence == pytest.approx(0.95)
    assert first.metadata == {"language": "zh", "segment_index": 0}
    assert events[1].id == "asr-000001"
    assert events[1].start_ms == 1500 and events[1].end_ms == 3000


def test_segments_to_events_skips_empty_text() -> None:
    """空文本/纯空白分段不产生事件。"""
    segments = [
        AsrSegment(text="", start_ms=0, end_ms=500, confidence=0.9),
        AsrSegment(text="   ", start_ms=500, end_ms=1000, confidence=0.9),
        AsrSegment(text="有效", start_ms=1000, end_ms=1500, confidence=0.9),
    ]
    events = segments_to_events(VIDEO_ID, segments, "zh")
    assert len(events) == 1
    assert events[0].content == "有效"


def test_segments_to_events_clamps_out_of_range_confidence() -> None:
    """引擎返回越界置信度时截断到 [0,1]，不导致事件校验失败。"""
    segments = [
        AsrSegment(text="偏高", start_ms=0, end_ms=1, confidence=1.5),
        AsrSegment(text="偏低", start_ms=1, end_ms=2, confidence=-0.3),
    ]
    events = segments_to_events(VIDEO_ID, segments, "zh")
    assert events[0].confidence == 1.0
    assert events[1].confidence == 0.0


def test_run_asr_writes_result_file() -> None:
    """run_asr 聚合结果落盘 asr.json 并返回事件列表。"""
    events = run_asr(VIDEO_ID, provider=FakeProvider())

    assert len(events) == 2
    result_file = get_settings().outputs_path / VIDEO_ID / "asr.json"
    assert result_file.is_file()
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["video_id"] == VIDEO_ID
    assert payload["modality"] == "asr"
    assert len(payload["events"]) == 2
    assert payload["events"][0]["start_ms"] == 0
    assert payload["events"][0]["end_ms"] == 1200


def test_run_asr_empty_segments_produces_empty_result() -> None:
    """无有效分段时产出空事件列表，asr.json 仍正常落盘。"""
    provider = FakeProvider(AsrResult(segments=[], language="zh"))
    events = run_asr(VIDEO_ID, provider=provider)
    assert events == []

    payload = json.loads(
        (get_settings().outputs_path / VIDEO_ID / "asr.json").read_text(encoding="utf-8")
    )
    assert payload["events"] == []


def test_run_asr_default_provider_uses_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 provider 时走工厂单例（此处以桩验证接线，不触发真实服务）。"""
    called: list[bool] = []

    def fake_get_default_provider():
        called.append(True)
        return FakeProvider()

    monkeypatch.setattr(asr_pipeline, "get_default_provider", fake_get_default_provider)
    events = run_asr(VIDEO_ID)
    assert called == [True]
    assert len(events) == 2


def test_run_asr_audio_extract_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """音频提取失败（如无音轨）时错误原样抛出，不产出 asr.json。"""
    from app.services.audio import NoAudioTrackError

    def raise_no_audio(video_id: str) -> Path:
        raise NoAudioTrackError("视频不含音轨")

    monkeypatch.setattr(asr_pipeline, "ensure_audio", raise_no_audio)
    with pytest.raises(NoAudioTrackError):
        run_asr(VIDEO_ID, provider=FakeProvider())
    assert not (get_settings().outputs_path / VIDEO_ID / "asr.json").exists()


def test_pipeline_does_not_import_concrete_provider() -> None:
    """防回归：业务层只允许依赖 AsrProvider 抽象与工厂入口，不得 import 具体实现模块。"""
    import ast

    source = Path(asr_pipeline.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "ai.asr.qwen_asr" not in imported_modules
    assert not any(
        module.startswith("ai.asr.") and module not in {"ai.asr.base", "ai.asr.factory"}
        for module in imported_modules
    ), f"asr_pipeline 引入了抽象与工厂以外的 ai.asr 模块：{imported_modules}"
