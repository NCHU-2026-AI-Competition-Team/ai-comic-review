"""ASR 相关 HTTP 接口的 API 级测试。

通过 monkeypatch 隔离存储目录、音频提取与 ASR Provider，
不依赖真实 Modal 服务与 ffmpeg。
"""

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from ai.asr.base import (
    AsrNotConfiguredError,
    AsrResponseFormatError,
    AsrResult,
    AsrSegment,
    AsrServiceError,
    AsrTimeoutError,
)
from app.core.config import get_settings
from app.main import app
from app.schemas.video import VideoJob
from app.services import asr_pipeline, audio, registry

VALID_VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """每个用例使用独立的临时存储目录，并桩掉音频提取。"""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    fake_audio = tmp_path / "audio.wav"
    fake_audio.write_bytes(b"RIFF" + b"\x00" * 40)
    monkeypatch.setattr(asr_pipeline, "ensure_audio", lambda video_id: fake_audio)
    yield TestClient(app)
    get_settings.cache_clear()


def _save_job(video_id: str = VALID_VIDEO_ID, status: str = "processed") -> None:
    registry.save_job(VideoJob(video_id=video_id, filename="demo.mp4", status=status))  # type: ignore[arg-type]


class FakeProvider:
    """返回固定识别结果的桩引擎。"""

    def transcribe(self, audio_path: Path) -> AsrResult:
        return AsrResult(
            segments=[
                AsrSegment(text="第一句台词", start_ms=0, end_ms=1200, confidence=0.95),
                AsrSegment(text="第二句台词", start_ms=1500, end_ms=3000, confidence=0.85),
            ],
            language="zh",
        )


def test_run_asr_video_not_found(client: TestClient) -> None:
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 404
    assert "视频不存在" in response.json()["detail"]


def test_run_asr_invalid_video_id(client: TestClient) -> None:
    response = client.post("/api/videos/not-a-valid-id/asr")
    assert response.status_code == 400


def test_run_asr_no_audio_track_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """视频无音轨时返回 409 并提示无法执行语音识别。"""
    _save_job()

    def raise_no_audio(video_id: str) -> Path:
        raise audio.NoAudioTrackError("视频不含音轨")

    monkeypatch.setattr(asr_pipeline, "ensure_audio", raise_no_audio)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 409
    assert "音轨" in response.json()["detail"]


def test_run_asr_upload_missing_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原始视频文件缺失时返回 409 并提示重新上传。"""
    _save_job()

    def raise_missing(video_id: str) -> Path:
        raise audio.UploadNotFoundError("找不到上传文件")

    monkeypatch.setattr(asr_pipeline, "ensure_audio", raise_missing)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 409
    assert "重新上传" in response.json()["detail"]


def test_run_asr_and_get_events_roundtrip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """桩引擎：POST /asr 生成事件，GET /events?modality=asr 查询闭环。"""
    _save_job()
    monkeypatch.setattr(asr_pipeline, "get_default_provider", lambda: FakeProvider())

    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "video_id": VALID_VIDEO_ID,
        "modality": "asr",
        "event_count": 2,
        "reused": False,
    }

    # asr.json 已落盘
    result_file = get_settings().outputs_path / VALID_VIDEO_ID / "asr.json"
    assert result_file.is_file()

    events_response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=asr")
    assert events_response.status_code == 200
    events = events_response.json()
    assert len(events) == 2
    first = events[0]
    assert first["modality"] == "asr"
    assert first["start_ms"] == 0 and first["end_ms"] == 1200
    assert first["content"] == "第一句台词"
    assert first["confidence"] == pytest.approx(0.95)
    assert first["metadata"]["language"] == "zh"


def test_run_asr_second_call_reuses(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复触发默认复用已有 asr.json，响应标注 reused=true。"""
    _save_job()
    calls = {"count": 0}

    class CountingProvider:
        def transcribe(self, audio_path: Path) -> AsrResult:
            calls["count"] += 1
            return FakeProvider().transcribe(audio_path)

    monkeypatch.setattr(asr_pipeline, "get_default_provider", lambda: CountingProvider())

    first = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert first.status_code == 200
    assert first.json()["reused"] is False
    second = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert second.status_code == 200
    assert second.json() == {
        "video_id": VALID_VIDEO_ID,
        "modality": "asr",
        "event_count": 2,
        "reused": True,
    }
    assert calls["count"] == 1


def test_run_asr_force_true_reruns(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """force=true 时忽略已有结果重新识别。"""
    _save_job()
    calls = {"count": 0}

    class CountingProvider:
        def transcribe(self, audio_path: Path) -> AsrResult:
            calls["count"] += 1
            return FakeProvider().transcribe(audio_path)

    monkeypatch.setattr(asr_pipeline, "get_default_provider", lambda: CountingProvider())
    assert client.post(f"/api/videos/{VALID_VIDEO_ID}/asr").status_code == 200
    forced = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr?force=true")
    assert forced.status_code == 200
    assert forced.json()["reused"] is False
    assert calls["count"] == 2


def test_run_asr_timeout_returns_504(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()

    def raise_timeout(*args: object, **kwargs: object):
        raise AsrTimeoutError("云端超时")

    monkeypatch.setattr(asr_pipeline, "run_asr", raise_timeout)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 504
    assert "超时" in response.json()["detail"]


def test_run_asr_service_error_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()

    def raise_service(*args: object, **kwargs: object):
        raise AsrServiceError("云端 500")

    monkeypatch.setattr(asr_pipeline, "run_asr", raise_service)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 502


def test_run_asr_format_error_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()

    def raise_format(*args: object, **kwargs: object):
        raise AsrResponseFormatError("结构非法")

    monkeypatch.setattr(asr_pipeline, "run_asr", raise_format)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 502


def test_run_asr_not_configured_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()

    def raise_unconfigured(*args: object, **kwargs: object):
        raise AsrNotConfiguredError("未配置")

    monkeypatch.setattr(asr_pipeline, "run_asr", raise_unconfigured)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 503
    assert "未配置" in response.json()["detail"]


def test_run_asr_audio_timeout_returns_504(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()

    def raise_audio_timeout(*args: object, **kwargs: object):
        raise audio.AudioExtractTimeoutError("ffmpeg 超时")

    monkeypatch.setattr(asr_pipeline, "run_asr", raise_audio_timeout)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/asr")
    assert response.status_code == 504


def test_api_does_not_import_concrete_asr_provider() -> None:
    """防回归：API 层只依赖抽象异常，不得 import 具体 Provider 实现。"""
    import ast

    from app.api import videos

    source = Path(videos.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "ai.asr.qwen_asr" not in imported_modules
    assert "ai.asr.base" in imported_modules
