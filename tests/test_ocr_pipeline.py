"""OCR 流水线的纯逻辑单元测试。

不依赖 paddleocr/paddlepaddle：通过注入 fake Provider 隔离引擎，
覆盖文字行聚合、空结果、失败帧处理与帧清单异常分支。
"""

import json
from pathlib import Path

import pytest

from ai.ocr.base import OcrTextLine
from app.core.config import get_settings
from app.schemas.video import FrameInfo
from app.services import ocr_pipeline
from app.services.ocr_pipeline import (
    FramesCorruptedError,
    FramesNotFoundError,
    frame_lines_to_event,
    load_frames_info,
    run_ocr,
)

VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例使用独立的临时存储目录，并在结束后还原配置缓存。"""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _frame(frame_id: str = "frame_000000", timestamp_ms: int = 1500) -> FrameInfo:
    return FrameInfo(
        frame_id=frame_id,
        timestamp_ms=timestamp_ms,
        timestamp="00:00:01.500",
        path=f"/api/videos/{VIDEO_ID}/frames/{frame_id}.jpg",
    )


def _write_frames_json(frames: list[FrameInfo]) -> None:
    frames_dir = get_settings().frames_path / VIDEO_ID
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "frames.json").write_text(
        json.dumps(
            {
                "sampling": {"method": "fixed_fps", "fps": 2.0, "threshold": None},
                "count": len(frames),
                "frames": [f.model_dump(mode="json") for f in frames],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_frame_lines_to_event_aggregates_lines() -> None:
    """多行文字聚合：正文换行拼接、置信度取均值、时间戳与帧对齐、明细入 metadata。"""
    frame = _frame(timestamp_ms=2500)
    lines = [
        OcrTextLine(text="第一行", bbox=[[0, 0], [10, 0], [10, 10], [0, 10]], confidence=0.9),
        OcrTextLine(text="第二行", bbox=[[0, 20], [10, 20], [10, 30], [0, 30]], confidence=0.6),
    ]
    event = frame_lines_to_event(VIDEO_ID, frame, lines)

    assert event is not None
    assert event.id == "ocr-frame_000000"
    assert event.video_id == VIDEO_ID
    assert event.modality == "ocr"
    assert event.start_ms == 2500
    assert event.end_ms == 2500
    assert event.content == "第一行\n第二行"
    assert event.confidence == pytest.approx(0.75)
    assert event.metadata["frame_id"] == "frame_000000"
    assert len(event.metadata["lines"]) == 2
    assert event.metadata["lines"][0]["text"] == "第一行"
    assert event.metadata["lines"][0]["box"] == [[0, 0], [10, 0], [10, 10], [0, 10]]


def test_frame_lines_to_event_empty_lines_returns_none() -> None:
    """无文字行时不产生事件。"""
    assert frame_lines_to_event(VIDEO_ID, _frame(), []) is None


def test_load_frames_info_missing_raises_not_found() -> None:
    with pytest.raises(FramesNotFoundError):
        load_frames_info(VIDEO_ID)


def test_load_frames_info_corrupted_raises() -> None:
    frames_dir = get_settings().frames_path / VIDEO_ID
    frames_dir.mkdir(parents=True)
    (frames_dir / "frames.json").write_text("{ 不是合法 JSON", encoding="utf-8")
    with pytest.raises(FramesCorruptedError):
        load_frames_info(VIDEO_ID)


def test_run_ocr_skips_empty_and_failed_frames(tmp_path: Path) -> None:
    """无文本帧不产事件；识别失败的帧跳过且不中断；结果落盘 ocr.json。"""
    frames = [_frame("frame_000000", 0), _frame("frame_000001", 500), _frame("frame_000002", 1000)]
    _write_frames_json(frames)

    class FakeProvider:
        def recognize(self, image_path: Path) -> list[OcrTextLine]:
            name = image_path.name
            if "frame_000001" in name:
                raise RuntimeError("模拟识别失败")
            if "frame_000002" in name:
                return []
            return [OcrTextLine(text="你好", bbox=[[0, 0], [1, 0], [1, 1], [0, 1]], confidence=0.8)]

    outcome = run_ocr(VIDEO_ID, provider=FakeProvider())

    assert outcome.reused is False
    events = outcome.events
    assert len(events) == 1
    assert events[0].id == "ocr-frame_000000"
    assert events[0].content == "你好"

    result_file = get_settings().outputs_path / VIDEO_ID / "ocr.json"
    assert result_file.is_file()
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["video_id"] == VIDEO_ID
    assert payload["modality"] == "ocr"
    assert len(payload["events"]) == 1
    assert payload["events"][0]["start_ms"] == 0


def test_run_ocr_all_frames_empty_produces_empty_result(tmp_path: Path) -> None:
    """全部帧无文字时产出空事件列表，ocr.json 仍正常落盘。"""
    _write_frames_json([_frame()])

    class EmptyProvider:
        def recognize(self, image_path: Path) -> list[OcrTextLine]:
            return []

    outcome = run_ocr(VIDEO_ID, provider=EmptyProvider())
    assert outcome.reused is False
    assert outcome.events == []

    payload = json.loads(
        (get_settings().outputs_path / VIDEO_ID / "ocr.json").read_text(encoding="utf-8")
    )
    assert payload["events"] == []


def test_run_ocr_missing_frames_json_raises(tmp_path: Path) -> None:
    """未抽帧的视频执行 OCR 明确抛 FramesNotFoundError。"""

    class UnusedProvider:
        def recognize(self, image_path: Path) -> list[OcrTextLine]:
            raise AssertionError("不应被调用")

    with pytest.raises(FramesNotFoundError):
        run_ocr(VIDEO_ID, provider=UnusedProvider())


def test_run_ocr_default_provider_uses_singleton(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """不传 provider 时走工厂单例（此处以桩验证接线，不触发真实引擎）。"""
    _write_frames_json([_frame()])
    called: list[bool] = []

    class StubProvider:
        def recognize(self, image_path: Path) -> list[OcrTextLine]:
            return [OcrTextLine(text="stub", bbox=[[0, 0]], confidence=0.5)]

    def fake_get_default_provider():
        called.append(True)
        return StubProvider()

    monkeypatch.setattr(ocr_pipeline, "get_default_provider", fake_get_default_provider)
    outcome = run_ocr(VIDEO_ID)
    assert called == [True]
    assert len(outcome.events) == 1 and outcome.events[0].content == "stub"


def test_frame_lines_to_event_clamps_out_of_range_confidence() -> None:
    """引擎返回越界置信度（<0 或 >1）时截断到 [0,1]，不导致事件校验失败。"""
    lines = [
        OcrTextLine(text="偏高", bbox=[[0, 0]], confidence=1.5),
        OcrTextLine(text="偏低", bbox=[[0, 0]], confidence=-0.3),
    ]
    event = frame_lines_to_event(VIDEO_ID, _frame(), lines)

    assert event is not None
    assert event.confidence == pytest.approx(0.5)
    assert event.metadata["lines"][0]["confidence"] == 1.0
    assert event.metadata["lines"][1]["confidence"] == 0.0


def test_run_ocr_tolerates_out_of_range_confidence() -> None:
    """Provider 返回越界置信度时流水线不中断，事件正常落盘。"""

    class OutOfRangeProvider:
        def recognize(self, image_path: Path) -> list[OcrTextLine]:
            return [OcrTextLine(text="越界", bbox=[[0, 0]], confidence=2.0)]

    _write_frames_json([_frame()])
    outcome = run_ocr(VIDEO_ID, provider=OutOfRangeProvider())
    assert len(outcome.events) == 1
    assert outcome.events[0].confidence == 1.0


def _counting_provider(calls: dict):
    class CountingProvider:
        def recognize(self, image_path: Path) -> list[OcrTextLine]:
            calls["count"] += 1
            return [OcrTextLine(text="你好", bbox=[[0, 0]], confidence=0.8)]

    return CountingProvider()


def test_run_ocr_second_call_reuses_existing_result() -> None:
    """默认复用已有完好的 ocr.json 并标注 reused=True，不再调用引擎。"""
    _write_frames_json([_frame()])
    calls = {"count": 0}
    provider = _counting_provider(calls)

    first = run_ocr(VIDEO_ID, provider=provider)
    assert first.reused is False
    second = run_ocr(VIDEO_ID, provider=provider)
    assert second.reused is True
    assert second.events == first.events
    assert calls["count"] == 1


def test_run_ocr_force_true_reruns() -> None:
    """force=True 时忽略已有 ocr.json 重新识别。"""
    _write_frames_json([_frame()])
    calls = {"count": 0}
    provider = _counting_provider(calls)

    assert run_ocr(VIDEO_ID, provider=provider).reused is False
    forced = run_ocr(VIDEO_ID, provider=provider, force=True)
    assert forced.reused is False
    assert calls["count"] == 2


def test_run_ocr_corrupted_result_reruns() -> None:
    """已有 ocr.json 损坏时删除后重跑（与 ASR/VLM 的损坏恢复一致）。"""
    _write_frames_json([_frame()])
    calls = {"count": 0}
    provider = _counting_provider(calls)

    assert run_ocr(VIDEO_ID, provider=provider).reused is False
    result_file = get_settings().outputs_path / VIDEO_ID / "ocr.json"
    result_file.write_text("{ 不是合法 JSON", encoding="utf-8")

    outcome = run_ocr(VIDEO_ID, provider=provider)
    assert outcome.reused is False
    assert calls["count"] == 2


def test_pipeline_does_not_import_concrete_provider() -> None:
    """防回归：业务层只允许依赖 OcrProvider 抽象与工厂入口，不得 import 具体实现模块。"""
    import ast

    source = Path(ocr_pipeline.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "ai.ocr.paddleocr" not in imported_modules
    assert not any(
        module.startswith("ai.ocr.") and module not in {"ai.ocr.base", "ai.ocr.factory"}
        for module in imported_modules
    ), f"ocr_pipeline 引入了抽象与工厂以外的 ai.ocr 模块：{imported_modules}"
