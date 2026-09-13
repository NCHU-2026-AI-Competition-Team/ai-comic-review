"""视频上传相关 HTTP 接口的 API 级测试。

通过 monkeypatch 隔离存储目录与视频处理流水线，
不依赖真实 ffmpeg，也不污染仓库下的 storage/。
"""

import io
import json
import sys
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import app.services.video as video_service
from app.core.config import get_settings
from app.main import app
from app.schemas.video import VideoJob
from app.services import registry

VALID_VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """每个用例使用独立的临时存储目录，并在结束后还原配置缓存。"""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _upload(client: TestClient, filename: str = "clip.mp4", content: bytes = FAKE_MP4):
    return client.post("/api/videos", files={"file": (filename, io.BytesIO(content), "video/mp4")})


def test_upload_success_with_processing_pipeline(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """正常上传：处理流水线完成后返回 processed 状态与元数据。"""

    def fake_process(video_id: str, sampling: str = "fixed_fps") -> None:
        registry.update_job(video_id, status="processed")

    monkeypatch.setattr(video_service, "process_video", fake_process)

    response = _upload(client)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["filename"] == "clip.mp4"
    assert payload["status"] == "processed"
    assert payload["sampling"] == "fixed_fps"

    settings = get_settings()
    video_id = payload["video_id"]
    assert (settings.uploads_path / f"{video_id}.mp4").is_file()
    job = registry.get_job(video_id)
    assert job is not None and job.status == "processed"


def test_upload_default_sampling_is_fixed_fps(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 sampling 时默认 fixed_fps，并透传给处理流水线。"""
    received: list[str] = []

    def fake_process(video_id: str, sampling: str = "fixed_fps") -> None:
        received.append(sampling)
        registry.update_job(video_id, status="processed")

    monkeypatch.setattr(video_service, "process_video", fake_process)

    response = _upload(client)
    assert response.status_code == 201, response.text
    assert response.json()["sampling"] == "fixed_fps"
    assert received == ["fixed_fps"]


def test_upload_accepts_scene_sampling(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """sampling=scene 上传成功，任务记录与 GET /api/videos/{id} 均可见采样模式。"""
    received: list[str] = []

    def fake_process(video_id: str, sampling: str = "fixed_fps") -> None:
        received.append(sampling)
        registry.update_job(video_id, status="processed")

    monkeypatch.setattr(video_service, "process_video", fake_process)

    response = client.post(
        "/api/videos",
        files={"file": ("clip.mp4", io.BytesIO(FAKE_MP4), "video/mp4")},
        data={"sampling": "scene"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["sampling"] == "scene"
    assert received == ["scene"]

    video_id = payload["video_id"]
    job = registry.get_job(video_id)
    assert job is not None and job.sampling == "scene"

    get_response = client.get(f"/api/videos/{video_id}")
    assert get_response.status_code == 200
    assert get_response.json()["sampling"] == "scene"


def test_upload_rejects_invalid_sampling(client: TestClient) -> None:
    """非法 sampling 值返回 4xx，且不落盘任务记录。"""
    response = client.post(
        "/api/videos",
        files={"file": ("clip.mp4", io.BytesIO(FAKE_MP4), "video/mp4")},
        data={"sampling": "auto"},
    )
    assert response.status_code == 400
    assert "不支持的采样模式" in response.json()["detail"]

    settings = get_settings()
    assert list(settings.uploads_path.glob("*/job.json")) == []


def test_upload_keeps_processing_when_pipeline_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """处理模块缺失（ImportError）时保持 processing 状态，不影响上传。"""
    monkeypatch.setitem(sys.modules, "app.services.video", None)

    response = _upload(client)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "processing"
    assert payload["metadata"] is None
    assert payload["frames"] is None


def test_upload_returns_500_when_processing_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """处理流水线抛异常时返回 500，任务记录标记为 failed。"""

    def failing_process(video_id: str, sampling: str = "fixed_fps") -> None:
        raise RuntimeError("模拟处理失败")

    monkeypatch.setattr(video_service, "process_video", failing_process)

    response = _upload(client)
    assert response.status_code == 500
    assert "视频处理失败" in response.json()["detail"]

    settings = get_settings()
    saved = list(settings.uploads_path.glob("*/job.json"))
    assert len(saved) == 1
    job = VideoJob.model_validate(json.loads(saved[0].read_text(encoding="utf-8")))
    assert job.status == "failed"
    assert job.error == "模拟处理失败"


def test_upload_rejects_unsupported_extension(client: TestClient) -> None:
    response = _upload(client, filename="notes.txt")
    assert response.status_code == 400
    assert "不支持的视频格式" in response.json()["detail"]


def test_upload_rejects_missing_filename(client: TestClient) -> None:
    # 空文件名会被 multipart 层视为缺少文件字段，由 FastAPI 返回 422
    response = _upload(client, filename="")
    assert response.status_code == 422


def test_upload_rejects_oversized_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过大小上限时返回 413，且不残留部分文件与任务记录。"""
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "1")
    get_settings.cache_clear()

    big = io.BytesIO(b"\x00" * (2 * 1024 * 1024))
    response = client.post("/api/videos", files={"file": ("big.mp4", big, "video/mp4")})
    assert response.status_code == 413

    settings = get_settings()
    assert list(settings.uploads_path.glob("*.mp4")) == []
    assert list(settings.uploads_path.glob("*/job.json")) == []


def test_get_video_returns_job(client: TestClient) -> None:
    registry.save_job(VideoJob(video_id=VALID_VIDEO_ID, filename="demo.mp4"))
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["video_id"] == VALID_VIDEO_ID
    assert payload["status"] == "processing"


def test_get_video_not_found(client: TestClient) -> None:
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}")
    assert response.status_code == 404


def test_get_video_corrupted_job_returns_500(client: TestClient) -> None:
    """job.json 损坏时返回 500 并提示任务记录文件损坏，而不是当作 404。"""
    job_dir = get_settings().uploads_path / VALID_VIDEO_ID
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text("{ 不是合法 JSON", encoding="utf-8")

    response = client.get(f"/api/videos/{VALID_VIDEO_ID}")
    assert response.status_code == 500
    assert "任务记录文件损坏" in response.json()["detail"]


def test_get_video_rejects_invalid_id_format(client: TestClient) -> None:
    response = client.get("/api/videos/not-a-valid-id")
    assert response.status_code == 400
    assert "video_id 格式非法" in response.json()["detail"]


def test_get_frames_not_generated(client: TestClient) -> None:
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/frames")
    assert response.status_code == 404


def test_get_frames_success(client: TestClient) -> None:
    frames_dir = get_settings().frames_path / VALID_VIDEO_ID
    frames_dir.mkdir(parents=True)
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
                        "path": f"/api/videos/{VALID_VIDEO_ID}/frames/frame_000000.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/frames")
    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_get_frames_corrupted_file(client: TestClient) -> None:
    frames_dir = get_settings().frames_path / VALID_VIDEO_ID
    frames_dir.mkdir(parents=True)
    (frames_dir / "frames.json").write_text("{ 不是合法 JSON", encoding="utf-8")
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/frames")
    assert response.status_code == 500


def test_get_frame_image_success(client: TestClient) -> None:
    frames_dir = get_settings().frames_path / VALID_VIDEO_ID
    frames_dir.mkdir(parents=True)
    (frames_dir / "frame_000000.jpg").write_bytes(b"\xff\xd8\xff")
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/frames/frame_000000.jpg")
    assert response.status_code == 200
    assert response.content == b"\xff\xd8\xff"


def test_get_frame_image_not_found(client: TestClient) -> None:
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/frames/frame_000000.jpg")
    assert response.status_code == 404


def test_get_frame_image_rejects_disallowed_suffix(client: TestClient) -> None:
    frames_dir = get_settings().frames_path / VALID_VIDEO_ID
    frames_dir.mkdir(parents=True)
    (frames_dir / "notes.txt").write_text("secret", encoding="utf-8")
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/frames/notes.txt")
    assert response.status_code == 404
