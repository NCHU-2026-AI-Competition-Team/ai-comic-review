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
from app.services.modality_store import write_modality_events

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
    outcome = run_asr(VIDEO_ID, provider=FakeProvider())

    assert outcome.reused is False
    assert len(outcome.events) == 2
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
    outcome = run_asr(VIDEO_ID, provider=provider)
    assert outcome.events == []
    assert outcome.reused is False

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
    outcome = run_asr(VIDEO_ID)
    assert called == [True]
    assert len(outcome.events) == 2


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


def test_segments_to_events_sorts_out_of_order() -> None:
    """乱序分段按 (start_ms, end_ms) 稳定排序，metadata 保留原始下标。"""
    segments = [
        AsrSegment(text="后", start_ms=2000, end_ms=3000, confidence=0.9),
        AsrSegment(text="前", start_ms=0, end_ms=1000, confidence=0.9),
    ]
    events = segments_to_events(VIDEO_ID, segments, "zh")
    assert [event.content for event in events] == ["前", "后"]
    assert events[0].id == "asr-000001"
    assert events[0].metadata["segment_index"] == 1
    assert events[1].id == "asr-000000"
    assert events[1].metadata["segment_index"] == 0


def test_segments_to_events_stable_for_identical_timestamps() -> None:
    """相同时间戳保持输入相对顺序。"""
    segments = [
        AsrSegment(text="A", start_ms=100, end_ms=200, confidence=0.9),
        AsrSegment(text="B", start_ms=100, end_ms=200, confidence=0.8),
    ]
    events = segments_to_events(VIDEO_ID, segments, "zh")
    assert [event.content for event in events] == ["A", "B"]
    assert events[0].metadata["segment_index"] == 0
    assert events[1].metadata["segment_index"] == 1


def test_segments_to_events_allows_overlap() -> None:
    """重叠区间按起点排序后仍保留各自时间戳。"""
    segments = [
        AsrSegment(text="后段", start_ms=500, end_ms=1500, confidence=0.9),
        AsrSegment(text="前段", start_ms=0, end_ms=1000, confidence=0.9),
    ]
    events = segments_to_events(VIDEO_ID, segments, "zh")
    assert [event.content for event in events] == ["前段", "后段"]
    assert events[0].end_ms > events[1].start_ms


def test_run_asr_reuses_existing_result() -> None:
    """已有 asr.json 时默认复用，不再调用 Provider。"""
    first = run_asr(VIDEO_ID, provider=FakeProvider())
    assert first.reused is False

    class ExplodingProvider:
        def transcribe(self, audio_path: Path) -> AsrResult:
            raise AssertionError("复用已有结果时不应再次识别")

    second = run_asr(VIDEO_ID, provider=ExplodingProvider())
    assert second.reused is True
    assert [event.content for event in second.events] == [event.content for event in first.events]


def test_run_asr_force_reruns() -> None:
    """force=true 忽略已有 asr.json 重新识别并覆盖。"""
    run_asr(VIDEO_ID, provider=FakeProvider())
    replacement = AsrResult(
        segments=[AsrSegment(text="新台词", start_ms=0, end_ms=800, confidence=0.7)],
        language="zh",
    )
    outcome = run_asr(VIDEO_ID, provider=FakeProvider(replacement), force=True)
    assert outcome.reused is False
    assert len(outcome.events) == 1
    assert outcome.events[0].content == "新台词"
    payload = json.loads(
        (get_settings().outputs_path / VIDEO_ID / "asr.json").read_text(encoding="utf-8")
    )
    assert payload["events"][0]["content"] == "新台词"


def test_run_asr_corrupted_result_reruns() -> None:
    """损坏的 asr.json 删除后重新识别。"""
    result_file = get_settings().outputs_path / VIDEO_ID / "asr.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text("{ 不是合法 JSON", encoding="utf-8")

    outcome = run_asr(VIDEO_ID, provider=FakeProvider())
    assert outcome.reused is False
    assert len(outcome.events) == 2
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["modality"] == "asr"


def test_run_asr_atomic_write_leaves_no_tmp() -> None:
    """落盘使用临时文件原子替换，完成后不残留 .tmp。"""
    run_asr(VIDEO_ID, provider=FakeProvider())
    result_dir = get_settings().outputs_path / VIDEO_ID
    assert (result_dir / "asr.json").is_file()
    assert not (result_dir / "asr.json.tmp").exists()
    write_modality_events(VIDEO_ID, "asr", [])
    assert not (result_dir / "asr.json.tmp").exists()
