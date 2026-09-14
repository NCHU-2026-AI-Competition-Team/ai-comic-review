"""人工复核决策 HTTP 接口的 API 级测试。

通过 monkeypatch 隔离存储目录，覆盖提交、覆盖更新、报告回显与错误码。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.schemas.video import VideoJob
from app.services import registry, verdict_store

VALID_VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _save_job(video_id: str = VALID_VIDEO_ID, status: str = "processed") -> None:
    registry.save_job(VideoJob(video_id=video_id, filename="demo.mp4", status=status))  # type: ignore[arg-type]


def _verdict_file(video_id: str = VALID_VIDEO_ID) -> Path:
    return get_settings().outputs_path / video_id / "verdict.json"


def _write_vlm_json(video_id: str = VALID_VIDEO_ID) -> None:
    """写入最小合法 vlm.json，使 GET /report 可返回结构化报告。"""
    out_dir = get_settings().outputs_path / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "vlm.json").write_text(
        json.dumps(
            {
                "video_id": video_id,
                "modality": "vlm",
                "events": [
                    {
                        "id": "vlm-0000",
                        "video_id": video_id,
                        "modality": "vlm",
                        "start_ms": 0,
                        "end_ms": 500,
                        "content": "画面冲突",
                        "confidence": 0.95,
                        "metadata": {
                            "risk": True,
                            "category": "violence",
                            "severity": "high",
                            "evidence": "第1帧含暴力",
                            "reason": "画面冲突",
                            "suggestion": "拦截",
                            "needs_escalation": True,
                            "escalation_status": "pending_review",
                            "batch_index": 0,
                            "review_start_ms": 0,
                            "review_end_ms": 500,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _assert_iso8601(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed


def test_submit_verdict_success(client: TestClient) -> None:
    _save_job()
    response = client.post(
        f"/api/videos/{VALID_VIDEO_ID}/verdict",
        json={"decision": "approve", "note": "", "event_id": None},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["video_id"] == VALID_VIDEO_ID
    assert payload["decision"] == "approve"
    assert payload["note"] == ""
    assert payload["event_id"] is None
    _assert_iso8601(payload["created_at"])
    assert "updated_at" not in payload

    path = _verdict_file()
    assert path.is_file()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["decision"] == "approve"
    assert stored["updated_at"]
    assert not path.with_name("verdict.json.tmp").exists()


def test_submit_verdict_overwrite_preserves_created_at(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_job()
    times = [
        datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 10, 5, 0, tzinfo=timezone.utc),
    ]
    clock = iter(times)
    monkeypatch.setattr(verdict_store, "_now", lambda: next(clock))

    first = client.post(
        f"/api/videos/{VALID_VIDEO_ID}/verdict",
        json={"decision": "approve", "note": "先过", "event_id": None},
    )
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert first_payload["created_at"] == "2026-09-14T10:00:00Z" or first_payload[
        "created_at"
    ].startswith("2026-09-14T10:00:00")

    second = client.post(
        f"/api/videos/{VALID_VIDEO_ID}/verdict",
        json={"decision": "reject", "note": "改判拦截", "event_id": "vlm-0000"},
    )
    assert second.status_code == 200, second.text
    payload = second.json()
    assert payload["decision"] == "reject"
    assert payload["note"] == "改判拦截"
    assert payload["event_id"] == "vlm-0000"
    assert payload["created_at"] == first_payload["created_at"]
    assert "updated_at" not in payload

    stored = json.loads(_verdict_file().read_text(encoding="utf-8"))
    assert stored["decision"] == "reject"
    assert stored["created_at"] == first_payload["created_at"] or stored["created_at"].startswith(
        "2026-09-14T10:00:00"
    )
    assert stored["updated_at"].startswith("2026-09-14T10:05:00")
    assert stored["event_id"] == "vlm-0000"


def test_get_report_verdict_null_when_not_submitted(client: TestClient) -> None:
    _save_job()
    _write_vlm_json()
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/report")
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] is None


def test_get_report_includes_verdict_after_submit(client: TestClient) -> None:
    _save_job()
    _write_vlm_json()
    submitted = client.post(
        f"/api/videos/{VALID_VIDEO_ID}/verdict",
        json={"decision": "false_positive", "note": "误报", "event_id": None},
    )
    assert submitted.status_code == 200, submitted.text

    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/report")
    assert response.status_code == 200, response.text
    verdict = response.json()["verdict"]
    assert verdict is not None
    assert verdict["video_id"] == VALID_VIDEO_ID
    assert verdict["decision"] == "false_positive"
    assert verdict["note"] == "误报"
    assert verdict["event_id"] is None
    assert verdict["created_at"] == submitted.json()["created_at"]
    assert "updated_at" not in verdict


def test_submit_verdict_invalid_decision_returns_422(client: TestClient) -> None:
    _save_job()
    response = client.post(
        f"/api/videos/{VALID_VIDEO_ID}/verdict",
        json={"decision": "maybe", "note": "", "event_id": None},
    )
    assert response.status_code == 422


def test_submit_verdict_video_not_found(client: TestClient) -> None:
    response = client.post(
        f"/api/videos/{VALID_VIDEO_ID}/verdict",
        json={"decision": "approve", "note": "", "event_id": None},
    )
    assert response.status_code == 404
    assert "视频不存在" in response.json()["detail"]


def test_submit_verdict_invalid_video_id(client: TestClient) -> None:
    response = client.post(
        "/api/videos/not-a-valid-id/verdict",
        json={"decision": "approve", "note": "", "event_id": None},
    )
    assert response.status_code == 400
