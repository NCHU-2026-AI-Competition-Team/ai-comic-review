"""视频处理服务测试：元数据解析、抽帧与上传流程集成。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import registry, video

client = TestClient(app)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe 不可用")

FFPROBE_SAMPLE = {
    "streams": [
        {"codec_type": "audio", "codec_name": "aac"},
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30000/1001",
        },
    ],
    "format": {"duration": "12.345678", "bit_rate": "1500000"},
}


def test_parse_fraction() -> None:
    assert video._parse_fraction("30/1") == 30.0
    assert video._parse_fraction("30000/1001") == pytest.approx(29.970, rel=1e-3)
    assert video._parse_fraction("25") == 25.0
    assert video._parse_fraction("0/0") is None
    assert video._parse_fraction("") is None
    assert video._parse_fraction("abc/def") is None


def test_parse_probe_output() -> None:
    metadata = video._parse_probe_output(FFPROBE_SAMPLE)
    assert metadata.duration == pytest.approx(12.346, abs=1e-3)
    assert metadata.width == 1920
    assert metadata.height == 1080
    assert metadata.fps == pytest.approx(29.970, rel=1e-3)
    assert metadata.codec == "h264"
    assert metadata.bitrate == 1500000


def test_parse_probe_output_fallback_to_r_frame_rate() -> None:
    data = json.loads(json.dumps(FFPROBE_SAMPLE))
    data["streams"][1]["avg_frame_rate"] = "0/0"
    metadata = video._parse_probe_output(data)
    assert metadata.fps == pytest.approx(29.970, rel=1e-3)


def test_parse_probe_output_empty() -> None:
    metadata = video._parse_probe_output({})
    assert metadata.duration is None
    assert metadata.codec is None


def test_format_timestamp() -> None:
    assert video._format_timestamp(0) == "00:00:00.000"
    assert video._format_timestamp(83456) == "00:01:23.456"
    assert video._format_timestamp(3661007) == "01:01:01.007"


def _make_test_video(path: Path, duration: float = 2.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size=320x240:rate=10",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@requires_ffmpeg
def test_probe_metadata_real(tmp_path: Path) -> None:
    video_file = tmp_path / "sample.mp4"
    _make_test_video(video_file)
    metadata = video.probe_metadata(video_file)
    assert metadata.duration == pytest.approx(2.0, abs=0.2)
    assert metadata.width == 320
    assert metadata.height == 240
    assert metadata.fps == pytest.approx(10.0, abs=0.5)
    assert metadata.codec is not None


@requires_ffmpeg
def test_extract_frames_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    video_file = tmp_path / "sample.mp4"
    _make_test_video(video_file, duration=2.0)

    video_id = "12345678-1234-1234-1234-1234567890ab"
    info = video.extract_frames(video_id, video_file, fps=2.0)

    assert info.count == 4
    assert [f.timestamp_ms for f in info.frames] == [0, 500, 1000, 1500]
    assert info.frames[0].timestamp == "00:00:00.000"
    assert info.frames[0].path == f"/api/videos/{video_id}/frames/{info.frames[0].frame_id}.jpg"

    out_dir = tmp_path / "frames" / video_id
    frames_json = json.loads((out_dir / "frames.json").read_text(encoding="utf-8"))
    assert frames_json["count"] == 4
    for frame in info.frames:
        assert (out_dir / f"{frame.frame_id}.jpg").is_file()


@requires_ffmpeg
def test_upload_video_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()
    video_file = tmp_path / "clip.mp4"
    _make_test_video(video_file, duration=2.0)

    with video_file.open("rb") as f:
        response = client.post(
            "/api/videos",
            files={"file": ("clip.mp4", f, "video/mp4")},
        )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "processed"
    assert payload["metadata"]["width"] == 320
    assert payload["frames"]["count"] > 0

    video_id = payload["video_id"]
    job = registry.get_job(video_id)
    assert job is not None and job.status == "processed"

    frames_response = client.get(f"/api/videos/{video_id}/frames")
    assert frames_response.status_code == 200
    assert frames_response.json()["count"] == payload["frames"]["count"]

    frame_name = payload["frames"]["frames"][0]["frame_id"] + ".jpg"
    image_response = client.get(f"/api/videos/{video_id}/frames/{frame_name}")
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/jpeg"


def test_upload_rejects_bad_extension() -> None:
    response = client.post(
        "/api/videos",
        files={"file": ("notes.txt", b"not a video", "text/plain")},
    )
    assert response.status_code == 400
