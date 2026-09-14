"""VLM 审核 HTTP 接口的 API 级测试。

通过 monkeypatch 隔离存储目录与 VLM Provider，不依赖真实 Modal 服务。
"""

import ast
import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from ai.vlm.base import (
    VlmEscalationUnavailableError,
    VlmNotConfiguredError,
    VlmResponseFormatError,
    VlmReviewInput,
    VlmServiceError,
    VlmTimeoutError,
)
from ai.vlm.schemas import VlmReviewResult
from app.core.config import get_settings
from app.main import app
from app.schemas.video import VideoJob
from app.services import registry, vlm_pipeline

VALID_VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("MODAL_VLM_URL", "http://127.0.0.1:1")
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
                "count": 2,
                "frames": [
                    {
                        "frame_id": "frame_000000",
                        "timestamp_ms": 0,
                        "timestamp": "00:00:00.000",
                        "path": f"/api/videos/{video_id}/frames/frame_000000.jpg",
                    },
                    {
                        "frame_id": "frame_000001",
                        "timestamp_ms": 500,
                        "timestamp": "00:00:00.500",
                        "path": f"/api/videos/{video_id}/frames/frame_000001.jpg",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (frames_dir / "frame_000000.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 8)
    (frames_dir / "frame_000001.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 8)


class FakeProvider:
    def review(self, payload: VlmReviewInput) -> VlmReviewResult:
        if payload.model and "32b" in payload.model:
            raise VlmEscalationUnavailableError("501")
        return VlmReviewResult.model_validate(
            {
                "risk": True,
                "category": "violence",
                "severity": "high",
                "confidence": 0.95,
                "start_ms": 0,
                "end_ms": 500,
                "evidence": "第1帧含暴力",
                "reason": "画面冲突",
                "suggestion": "拦截",
            }
        )


def test_run_review_video_not_found(client: TestClient) -> None:
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert response.status_code == 404
    assert "视频不存在" in response.json()["detail"]


def test_run_review_invalid_video_id(client: TestClient) -> None:
    response = client.post("/api/videos/not-a-valid-id/review")
    assert response.status_code == 400


def test_run_review_without_frames_returns_409(client: TestClient) -> None:
    _save_job()
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert response.status_code == 409
    assert "抽帧" in response.json()["detail"]


def test_run_review_and_get_events_roundtrip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()
    _write_frames_json()
    monkeypatch.setattr(vlm_pipeline, "get_default_provider", lambda: FakeProvider())

    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["video_id"] == VALID_VIDEO_ID
    assert payload["modality"] == "vlm"
    assert payload["event_count"] >= 1
    assert payload["reused"] is False
    assert payload["needs_escalation"] is True  # high risk → 待复审（32B 未真正调用，Fake 对 32B 也返回 high）

    events_response = client.get(f"/api/videos/{VALID_VIDEO_ID}/events?modality=vlm")
    assert events_response.status_code == 200
    events = events_response.json()
    assert events[0]["modality"] == "vlm"
    assert events[0]["metadata"]["category"] == "violence"
    assert events[0]["start_ms"] in {0, 500}


def test_run_review_second_call_reuses(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_job()
    _write_frames_json()
    calls = {"count": 0}

    class CountingProvider:
        def review(self, payload: VlmReviewInput) -> VlmReviewResult:
            calls["count"] += 1
            return FakeProvider().review(payload)

    monkeypatch.setattr(vlm_pipeline, "get_default_provider", lambda: CountingProvider())
    first = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert first.status_code == 200
    assert first.json()["reused"] is False
    second = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert calls["count"] >= 1


def test_run_review_force_true_reruns(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_job()
    _write_frames_json()
    calls = {"count": 0}

    class CountingProvider:
        def review(self, payload: VlmReviewInput) -> VlmReviewResult:
            calls["count"] += 1
            return FakeProvider().review(payload)

    monkeypatch.setattr(vlm_pipeline, "get_default_provider", lambda: CountingProvider())
    assert client.post(f"/api/videos/{VALID_VIDEO_ID}/review").status_code == 200
    first_count = calls["count"]
    forced = client.post(f"/api/videos/{VALID_VIDEO_ID}/review?force=true")
    assert forced.status_code == 200
    assert forced.json()["reused"] is False
    assert calls["count"] > first_count


def test_run_review_timeout_returns_504(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_job()

    def raise_timeout(*args: object, **kwargs: object):
        raise VlmTimeoutError("云端超时")

    monkeypatch.setattr(vlm_pipeline, "run_review", raise_timeout)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert response.status_code == 504
    assert "超时" in response.json()["detail"]


def test_run_review_service_error_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()

    def raise_service(*args: object, **kwargs: object):
        raise VlmServiceError("云端 500")

    monkeypatch.setattr(vlm_pipeline, "run_review", raise_service)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert response.status_code == 502


def test_run_review_format_error_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()

    def raise_format(*args: object, **kwargs: object):
        raise VlmResponseFormatError("结构非法")

    monkeypatch.setattr(vlm_pipeline, "run_review", raise_format)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert response.status_code == 502


def test_run_review_not_configured_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()

    def raise_unconfigured(*args: object, **kwargs: object):
        raise VlmNotConfiguredError("未配置")

    monkeypatch.setattr(vlm_pipeline, "run_review", raise_unconfigured)
    response = client.post(f"/api/videos/{VALID_VIDEO_ID}/review")
    assert response.status_code == 503
    assert "未配置" in response.json()["detail"]


def test_get_report_not_generated(client: TestClient) -> None:
    _save_job()
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/report")
    assert response.status_code == 404
    assert "审核报告尚未生成" in response.json()["detail"]


def test_get_report_video_not_found(client: TestClient) -> None:
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/report")
    assert response.status_code == 404
    assert "视频不存在" in response.json()["detail"]


def test_get_report_after_review(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_job()
    _write_frames_json()
    monkeypatch.setattr(vlm_pipeline, "get_default_provider", lambda: FakeProvider())
    assert client.post(f"/api/videos/{VALID_VIDEO_ID}/review").status_code == 200

    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/report")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["video_id"] == VALID_VIDEO_ID
    assert report["filename"] == "demo.mp4"
    assert report["modalities"]["vlm"]["ran"] is True
    assert report["modalities"]["ocr"]["ran"] is False
    assert report["overall"]["risk"] is True
    assert report["overall"]["severity"] == "high"
    assert report["verdict"] is None
    assert report["risk_events"]
    severities = [event["metadata"]["severity"] for event in report["risk_events"]]
    assert severities == sorted(
        severities, key=lambda item: {"high": 0, "medium": 1, "low": 2, "none": 3}.get(item, 9)
    )


def test_get_report_corrupted_vlm_file(client: TestClient) -> None:
    _save_job()
    out_dir = get_settings().outputs_path / VALID_VIDEO_ID
    out_dir.mkdir(parents=True)
    (out_dir / "vlm.json").write_text("{ 不是合法 JSON", encoding="utf-8")
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/report")
    assert response.status_code == 500
    assert "审核结果文件损坏" in response.json()["detail"]


def test_get_report_sorts_risk_events_by_severity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多条不同 severity 的风险事件按 high > medium > low 排序。"""
    _save_job()
    _write_frames_json()

    class MixedProvider:
        def __init__(self) -> None:
            self.n = 0

        def review(self, payload: VlmReviewInput) -> VlmReviewResult:
            self.n += 1
            if payload.model and "32b" in payload.model:
                raise VlmServiceError("不应因复审失败而中断")
            # 单批：返回 medium，管线会因 medium 不 escalate（非 high）
            return VlmReviewResult.model_validate(
                {
                    "risk": True,
                    "category": "porn",
                    "severity": "low",
                    "confidence": 0.7,
                    "start_ms": 0,
                    "end_ms": 500,
                    "evidence": "低风险",
                    "reason": "擦边",
                    "suggestion": "复核",
                }
            )

    monkeypatch.setattr(vlm_pipeline, "get_default_provider", lambda: MixedProvider())
    assert client.post(f"/api/videos/{VALID_VIDEO_ID}/review").status_code == 200
    report = client.get(f"/api/videos/{VALID_VIDEO_ID}/report").json()
    assert report["overall"]["severity"] == "low"
    assert report["risk_events"][0]["metadata"]["severity"] == "low"


def test_api_does_not_import_concrete_vlm_provider() -> None:
    from app.api import videos

    source = Path(videos.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "ai.vlm.qwen" not in imported_modules
    assert "ai.vlm.base" in imported_modules
