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


def _stub_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_process(video_id: str, sampling: str = "fixed_fps") -> None:
        registry.update_job(video_id, status="processed")

    monkeypatch.setattr(video_service, "process_video", fake_process)


def test_upload_frame_fps_override_persisted_and_echoed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """frame_fps 覆盖写入任务记录，GET /api/videos/{id} 回显。"""
    _stub_process(monkeypatch)
    response = client.post(
        "/api/videos",
        files={"file": ("clip.mp4", io.BytesIO(FAKE_MP4), "video/mp4")},
        data={"sampling": "fixed_fps", "frame_fps": "5"},
    )
    assert response.status_code == 201, response.text
    video_id = response.json()["video_id"]
    job = registry.get_job(video_id)
    assert job is not None
    assert job.frame_fps == pytest.approx(5.0)
    assert job.scene_threshold is None
    assert job.scene_max_frames is None

    echoed = client.get(f"/api/videos/{video_id}")
    assert echoed.status_code == 200
    payload = echoed.json()
    assert payload["frame_fps"] == pytest.approx(5.0)
    assert payload["scene_threshold"] is None
    assert payload["scene_max_frames"] is None


def test_upload_scene_threshold_override_persisted_and_echoed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scene_threshold 覆盖写入任务记录，GET 回显。"""
    _stub_process(monkeypatch)
    response = client.post(
        "/api/videos",
        files={"file": ("clip.mp4", io.BytesIO(FAKE_MP4), "video/mp4")},
        data={"sampling": "scene", "scene_threshold": "0.25"},
    )
    assert response.status_code == 201, response.text
    video_id = response.json()["video_id"]
    job = registry.get_job(video_id)
    assert job is not None
    assert job.sampling == "scene"
    assert job.scene_threshold == pytest.approx(0.25)
    assert job.frame_fps is None

    echoed = client.get(f"/api/videos/{video_id}").json()
    assert echoed["scene_threshold"] == pytest.approx(0.25)
    assert echoed["scene_max_frames"] is None
    assert echoed["frame_fps"] is None


def test_upload_scene_max_frames_override_persisted_and_echoed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scene_max_frames 覆盖写入任务记录，GET 回显。"""
    _stub_process(monkeypatch)
    response = client.post(
        "/api/videos",
        files={"file": ("clip.mp4", io.BytesIO(FAKE_MP4), "video/mp4")},
        data={"sampling": "scene", "scene_max_frames": "12"},
    )
    assert response.status_code == 201, response.text
    video_id = response.json()["video_id"]
    job = registry.get_job(video_id)
    assert job is not None
    assert job.scene_max_frames == 12
    assert job.scene_threshold is None

    echoed = client.get(f"/api/videos/{video_id}").json()
    assert echoed["scene_max_frames"] == 12
    assert echoed["scene_threshold"] is None


def test_upload_omitted_overrides_fallback_to_global(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未提供覆盖字段时任务记录为 null，向后兼容现有调用。"""
    _stub_process(monkeypatch)
    response = _upload(client)
    assert response.status_code == 201, response.text
    video_id = response.json()["video_id"]
    job = registry.get_job(video_id)
    assert job is not None
    assert job.frame_fps is None
    assert job.scene_threshold is None
    assert job.scene_max_frames is None

    echoed = client.get(f"/api/videos/{video_id}").json()
    assert echoed["frame_fps"] is None
    assert echoed["scene_threshold"] is None
    assert echoed["scene_max_frames"] is None


def test_upload_empty_override_fields_treated_as_omitted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空白覆盖字段视为未提供，回退全局配置。"""
    _stub_process(monkeypatch)
    response = client.post(
        "/api/videos",
        files={"file": ("clip.mp4", io.BytesIO(FAKE_MP4), "video/mp4")},
        data={"sampling": "fixed_fps", "frame_fps": "  "},
    )
    assert response.status_code == 201, response.text
    job = registry.get_job(response.json()["video_id"])
    assert job is not None and job.frame_fps is None


@pytest.mark.parametrize(
    ("form", "needle"),
    [
        ({"sampling": "fixed_fps", "frame_fps": "abc"}, "frame_fps"),
        ({"sampling": "fixed_fps", "frame_fps": "0"}, "frame_fps"),
        ({"sampling": "fixed_fps", "frame_fps": "-1.5"}, "frame_fps"),
        ({"sampling": "scene", "scene_threshold": "xyz"}, "scene_threshold"),
        ({"sampling": "scene", "scene_threshold": "0"}, "scene_threshold"),
        ({"sampling": "scene", "scene_threshold": "1"}, "scene_threshold"),
        ({"sampling": "scene", "scene_threshold": "1.5"}, "scene_threshold"),
        ({"sampling": "scene", "scene_max_frames": "1.5"}, "scene_max_frames"),
        ({"sampling": "scene", "scene_max_frames": "0"}, "scene_max_frames"),
        ({"sampling": "scene", "scene_max_frames": "-3"}, "scene_max_frames"),
        ({"sampling": "scene", "scene_max_frames": "abc"}, "scene_max_frames"),
        ({"sampling": "fixed_fps", "scene_threshold": "0.4"}, "scene_threshold"),
        ({"sampling": "fixed_fps", "scene_max_frames": "10"}, "scene_max_frames"),
        ({"sampling": "scene", "frame_fps": "2"}, "frame_fps"),
    ],
)
def test_upload_rejects_invalid_sampling_overrides(
    client: TestClient, form: dict[str, str], needle: str
) -> None:
    """非法值、越界或与采样模式不匹配的覆盖字段返回 422 中文错误，且不落盘。"""
    response = client.post(
        "/api/videos",
        files={"file": ("clip.mp4", io.BytesIO(FAKE_MP4), "video/mp4")},
        data=form,
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert needle in detail

    settings = get_settings()
    assert list(settings.uploads_path.glob("*.mp4")) == []
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

def test_get_video_file_success(client: TestClient) -> None:
    upload_dir = get_settings().uploads_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / f"{VALID_VIDEO_ID}.mp4").write_bytes(b"test video content")
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file")
    assert response.status_code == 200
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.content == b"test video content"

def test_get_video_file_range_request(client: TestClient) -> None:
    upload_dir = get_settings().uploads_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / f"{VALID_VIDEO_ID}.mp4").write_bytes(b"test video content")
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": "bytes=5-9"})
    assert response.status_code == 206
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Length"] == "5"
    assert response.headers["Content-Range"] == "bytes 5-9/18"
    assert response.content == b"video"

def _write_uploaded_video(ext: str = ".mp4", content: bytes = b"test video content") -> None:
    upload_dir = get_settings().uploads_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / f"{VALID_VIDEO_ID}{ext}").write_bytes(content)

def test_get_video_file_range_suffix(client: TestClient) -> None:
    """bytes=-5 应返回文件最后 5 字节。"""
    _write_uploaded_video()
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": "bytes=-5"})
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 13-17/18"
    assert response.content == b"ntent"

def test_get_video_file_range_suffix_exceeds_size(client: TestClient) -> None:
    """suffix 超过文件大小时返回全量内容（206）。"""
    _write_uploaded_video()
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": "bytes=-500"})
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 0-17/18"
    assert response.content == b"test video content"

def test_get_video_file_range_open_end(client: TestClient) -> None:
    """bytes=5- 应返回到文件末尾。"""
    _write_uploaded_video()
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": "bytes=5-"})
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 5-17/18"
    assert response.content == b"video content"

def test_get_video_file_range_end_clipped(client: TestClient) -> None:
    """end 超出文件大小时裁剪到 file_size-1，返回 206 而非 416。"""
    _write_uploaded_video()
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": "bytes=5-50"})
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 5-17/18"
    assert response.content == b"video content"

def test_get_video_file_range_multi_ignored(client: TestClient) -> None:
    """多区间暂不实现，忽略 Range 返回 200 全量。"""
    _write_uploaded_video()
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": "bytes=0-1,3-4"})
    assert response.status_code == 200
    assert response.content == b"test video content"

def test_get_video_file_range_malformed_ignored(client: TestClient) -> None:
    """畸形 Range 值忽略并返回 200 全量。"""
    _write_uploaded_video()
    for header in ("bytes=abc-def", "bytes=-1-3"):
        response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": header})
        assert response.status_code == 200
        assert response.content == b"test video content"

def test_get_video_file_range_unsatisfiable(client: TestClient) -> None:
    """start 超出文件大小返回 416，且携带 Content-Range: bytes */{size}。"""
    _write_uploaded_video()
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": "bytes=100-200"})
    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */18"

def test_get_video_file_media_type_by_extension(client: TestClient) -> None:
    """.mov/.mkv 按后缀返回对应 MIME，Range 响应复用同一映射。"""
    _write_uploaded_video(ext=".mov")
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "video/quicktime"

    range_response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file", headers={"Range": "bytes=0-3"})
    assert range_response.status_code == 206
    assert range_response.headers["Content-Type"] == "video/quicktime"

    # find_uploaded_file 按 .mp4/.mov/.mkv 顺序探测，需先移除 .mov 文件
    (get_settings().uploads_path / f"{VALID_VIDEO_ID}.mov").unlink()
    _write_uploaded_video(ext=".mkv")
    mkv_response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file")
    assert mkv_response.status_code == 200
    assert mkv_response.headers["Content-Type"] == "video/x-matroska"

def test_get_video_file_not_found(client: TestClient) -> None:
    response = client.get(f"/api/videos/{VALID_VIDEO_ID}/file")
    assert response.status_code == 404
    assert "视频文件不存在" in response.json()["detail"]

def test_get_video_file_invalid_id_format(client: TestClient) -> None:
    response = client.get("/api/videos/not-a-valid-id/file")
    assert response.status_code == 400
    assert "video_id 格式非法" in response.json()["detail"]
