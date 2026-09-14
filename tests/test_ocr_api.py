"""OCR 相关 HTTP 接口的 API 级测试。

通过 monkeypatch 隔离存储目录与 OCR Provider，
不依赖真实 paddleocr 引擎与模型权重。
"""

import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from ai.ocr.base import OcrTextLine
from app.core.config import get_settings
from app.main import app
from app.services import ocr_pipeline, registry
from app.schemas.video import VideoJob

VALID_VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """每个用例使用独立的临时存储目录，并在结束后还原配置缓存。"""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _save_job(video_id: str = VALID_VIDEO_ID, status: str = "processed") -> None:
    registry.save_job(VideoJob(video_id=video_id, filename="demo.mp4", status=status))  # type: ignore[arg-type]


def _write_frames_json(video_id: str = VALID_VIDEO_ID) -> None:
    frames_dir = get_settings().frames_path / video_id
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "frames.json").write_text(
        json.dumps(
            {
                "sampling": {"method": "fixed_fps", "fps": 2.0, "threshold": None},
                "count": 1,
                "frames": [
                    {
                        "frame_id": "frame_000000",
                        "timestamp_ms": 0,
                        "timestamp": "00:00:00.000",
                        "path": f"/api/videos/{video_id}/frames/frame_000000.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class FakeProvider:
    """返回固定识别结果的桩引擎。"""

    def recognize(self, image_path: Path) -> list[OcrTextLine]:
        return [
            OcrTextLine(text="台词一", bbox=[[0, 0], [10, 0], [10, 10], [0, 10]], confidence=0.95),
            OcrTextLine(text="台词二", bbox=[[0, 20], [10, 20], [10, 30], [0, 30]], confidence=0.85),
        ]


def test_run_ocr_video_not_found(client: TestClient) -> None:
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/ocr")
    assert response.status_code == 404
    assert "视频不存在" in response.json()["detail"]


def test_run_ocr_invalid_video_id(client: TestClient) -> None:
    response = client.post("/api/videos/not-a-valid-id/ocr")
    assert response.status_code == 400


def test_run_ocr_without_frames_returns_409(client: TestClient) -> None:
    """已上传但未完成抽帧（无 frames.json）时返回 409 并提示先完成抽帧。"""
    _save_job()
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/ocr")
    assert response.status_code == 409
    assert "抽帧" in response.json()["detail"]


def test_run_ocr_corrupted_frames_returns_500(client: TestClient) -> None:
    _save_job()
    frames_dir = get_settings().frames_path / VALID_VIDEO_ID
    frames_dir.mkdir(parents=True)
    (frames_dir / "frames.json").write_text("{ 不是合法 JSON", encoding="utf-8")
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/ocr")
    assert response.status_code == 500
    assert "帧清单文件损坏" in response.json()["detail"]


def test_run_ocr_and_get_events_roundtrip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """假 frames.json + 桩引擎：POST /ocr 生成事件，GET /events 查询闭环。"""
    _save_job()
    _write_frames_json()
    monkeypatch.setattr(ocr_pipeline, "get_default_provider", lambda: FakeProvider())

    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/ocr")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "video_id": VALID_VIDEO_ID,
        "modality": "ocr",
        "event_count": 1,
        "reused": False,
    }

    # ocr.json 已落盘
    result_file = get_settings().outputs_path / VALID_VIDEO_ID / "ocr.json"
    assert result_file.is_file()

    events_response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=ocr")
    assert events_response.status_code == 200
    events = events_response.json()
    assert len(events) == 1
    event = events[0]
    assert event["modality"] == "ocr"
    assert event["start_ms"] == 0 and event["end_ms"] == 0
    assert event["content"] == "台词一\n台词二"
    assert event["confidence"] == pytest.approx(0.9)
    assert len(event["metadata"]["lines"]) == 2


def test_get_events_not_generated(client: TestClient) -> None:
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=ocr")
    assert response.status_code == 404
    assert "事件尚未生成" in response.json()["detail"]


def test_get_events_corrupted_file(client: TestClient) -> None:
    out_dir = get_settings().outputs_path / VALID_VIDEO_ID
    out_dir.mkdir(parents=True)
    (out_dir / "ocr.json").write_text("{ 不是合法 JSON", encoding="utf-8")
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=ocr")
    assert response.status_code == 500
    assert "事件文件损坏" in response.json()["detail"]


def test_get_events_invalid_modality(client: TestClient) -> None:
    """modality 超出枚举取值时由 FastAPI 校验返回 422。"""
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=text")
    assert response.status_code == 422


def _write_events_payload(payload: object, video_id: str = VALID_VIDEO_ID) -> None:
    out_dir = get_settings().outputs_path / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ocr.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _valid_event(video_id: str = VALID_VIDEO_ID) -> dict:
    return {
        "id": "ocr-frame_000000",
        "video_id": video_id,
        "modality": "ocr",
        "start_ms": 0,
        "end_ms": 0,
        "content": "台词",
        "confidence": 0.9,
        "metadata": {},
    }


def test_get_events_missing_top_level_fields(client: TestClient) -> None:
    """事件文件缺少顶层字段（video_id/modality）时返回 500 并提示文件损坏。"""
    _write_events_payload({"events": [_valid_event()]})
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=ocr")
    assert response.status_code == 500
    assert "事件文件损坏" in response.json()["detail"]


def test_get_events_video_id_mismatch(client: TestClient) -> None:
    """事件文件 video_id 与请求不一致时返回 500 并提示文件损坏。"""
    _write_events_payload(
        {
            "video_id": "00000000-0000-0000-0000-000000000000",
            "modality": "ocr",
            "events": [_valid_event("00000000-0000-0000-0000-000000000000")],
        }
    )
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=ocr")
    assert response.status_code == 500
    assert "事件文件损坏" in response.json()["detail"]


def test_get_events_modality_mismatch(client: TestClient) -> None:
    """事件文件 modality 与请求不一致时返回 500 并提示文件损坏。"""
    _write_events_payload(
        {
            "video_id": VALID_VIDEO_ID,
            "modality": "asr",
            "events": [{**_valid_event(), "modality": "asr"}],
        }
    )
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=ocr")
    assert response.status_code == 500
    assert "事件文件损坏" in response.json()["detail"]
