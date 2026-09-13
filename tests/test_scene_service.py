"""镜头切换检测测试：showinfo 解析、真实合成视频边界检测、结构与上限校验。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import registry, video
from app.services.video import scene

client = TestClient(app)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe 不可用")

VIDEO_ID = "12345678-1234-1234-1234-1234567890ab"

SHOWINFO_SAMPLE = """
[Parsed_showinfo_1 @ 000001ab] config in time_base: 1/1000, frame_rate: 10/1
[Parsed_showinfo_1 @ 000001ab] n:  20 pts: 2000 pts_time:2.000000 pos: 12345 fmt:yuv420p
[Parsed_showinfo_1 @ 000001ab] n:  40 pts: 4000 pts_time:4.5 pos: 23456 fmt:yuv420p
[Parsed_select_0 @ 000001cd] other line without timestamp
""".strip()


def _isolate_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    from app.core.config import get_settings

    get_settings.cache_clear()


def _make_scene_test_video(path: Path, segment_seconds: float = 2.0) -> None:
    """合成黑/白/黑三段拼接的测试视频，段间存在明确镜头切换。

    ffmpeg 场景分数基于亮度差异，纯色度差异（如红→绿）可能不得分，
    故使用亮度对比最大的黑白分段保证检测稳定触发。
    """
    duration = f"{segment_seconds}"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=black:size=320x240:rate=10:duration={duration}",
            "-f", "lavfi", "-i", f"color=white:size=320x240:rate=10:duration={duration}",
            "-f", "lavfi", "-i", f"color=black:size=320x240:rate=10:duration={duration}",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1[out]",
            "-map", "[out]",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_parse_showinfo_pts() -> None:
    """showinfo 输出解析：只取 showinfo 行的 pts_time，毫秒取整并去重排序。"""
    assert scene._parse_showinfo_pts(SHOWINFO_SAMPLE) == [2000, 4500]


def test_parse_showinfo_pts_empty() -> None:
    assert scene._parse_showinfo_pts("") == []
    assert scene._parse_showinfo_pts("没有任何时间戳的输出") == []


@requires_ffmpeg
def test_detect_scene_changes_real(tmp_path: Path) -> None:
    """三段各 2 秒的拼接视频应检出约 2 个边界，且接近 2000ms/4000ms。"""
    video_file = tmp_path / "scenes.mp4"
    _make_scene_test_video(video_file)

    boundaries = scene.detect_scene_changes(video_file, threshold=0.4)

    assert len(boundaries) == pytest.approx(2, abs=1)
    assert any(abs(ms - 2000) <= 500 for ms in boundaries)
    assert any(abs(ms - 4000) <= 500 for ms in boundaries)


@requires_ffmpeg
def test_extract_scene_frames_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """scene 模式抽帧：含起始帧共 3 帧左右，frames.json 结构与 fixed_fps 同构。"""
    _isolate_storage(tmp_path, monkeypatch)
    video_file = tmp_path / "scenes.mp4"
    _make_scene_test_video(video_file)

    info = scene.extract_scene_frames(VIDEO_ID, video_file)

    # 起始帧 + 2 个边界，容忍检测 ±1
    assert info.count == pytest.approx(3, abs=1)
    assert info.sampling.method == "scene_change"
    assert info.sampling.threshold == pytest.approx(0.4)
    assert info.sampling.fps is None

    assert info.frames[0].timestamp_ms == 0
    assert info.frames[0].timestamp == "00:00:00.000"
    boundary_ms = [f.timestamp_ms for f in info.frames[1:]]
    assert any(abs(ms - 2000) <= 500 for ms in boundary_ms)
    assert any(abs(ms - 4000) <= 500 for ms in boundary_ms)
    assert [f.frame_id for f in info.frames] == [f"frame_{i:06d}" for i in range(info.count)]

    out_dir = tmp_path / "frames" / VIDEO_ID
    for frame in info.frames:
        assert (out_dir / f"{frame.frame_id}.jpg").is_file()
        assert frame.path == f"/api/videos/{VIDEO_ID}/frames/{frame.frame_id}.jpg"

    scene_json = json.loads((out_dir / "frames.json").read_text(encoding="utf-8"))

    # 与 fixed_fps 模式产物做同构校验：顶层键与帧字段完全一致
    fixed_id = "22345678-1234-1234-1234-1234567890ab"
    fixed_info = video.extract_frames(fixed_id, video_file, fps=2.0)
    fixed_json = json.loads(
        (tmp_path / "frames" / fixed_id / "frames.json").read_text(encoding="utf-8")
    )
    assert fixed_info.sampling.method == "fixed_fps"
    assert set(scene_json.keys()) == set(fixed_json.keys())
    assert set(scene_json["sampling"].keys()) == set(fixed_json["sampling"].keys())
    for scene_frame, fixed_frame in zip(scene_json["frames"], fixed_json["frames"]):
        assert set(scene_frame.keys()) == set(fixed_frame.keys())


def test_extract_scene_frames_respects_max_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """镜头数量超过上限时截断，不产生海量帧。"""
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("SCENE_MAX_FRAMES", "5")
    from app.core.config import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(scene, "detect_scene_changes", lambda path, threshold: list(range(1000, 10000, 100)))
    monkeypatch.setattr(scene, "_require_tool", lambda name: name)

    def fake_extract(ffmpeg: str, video_path: Path, timestamp_ms: int, dest: Path) -> None:
        dest.write_bytes(b"\xff\xd8\xff")

    monkeypatch.setattr(scene, "_extract_single_frame", fake_extract)

    info = scene.extract_scene_frames(VIDEO_ID, Path("dummy.mp4"))

    assert info.count == 5
    assert info.frames[0].timestamp_ms == 0
    frames_json = json.loads((tmp_path / "frames" / VIDEO_ID / "frames.json").read_text(encoding="utf-8"))
    assert frames_json["count"] == 5


@requires_ffmpeg
def test_upload_video_scene_sampling_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """API 级：sampling=scene 上传后走镜头检测，响应与任务记录均带采样信息。"""
    _isolate_storage(tmp_path, monkeypatch)
    video_file = tmp_path / "scenes.mp4"
    _make_scene_test_video(video_file)

    with video_file.open("rb") as f:
        response = client.post(
            "/api/videos",
            files={"file": ("scenes.mp4", f, "video/mp4")},
            data={"sampling": "scene"},
        )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "processed"
    assert payload["sampling"] == "scene"
    assert payload["frames"]["sampling"]["method"] == "scene_change"
    assert payload["frames"]["sampling"]["threshold"] == pytest.approx(0.4)
    assert payload["frames"]["count"] == pytest.approx(3, abs=1)

    video_id = payload["video_id"]
    job = registry.get_job(video_id)
    assert job is not None and job.sampling == "scene"

    frames_response = client.get(f"/api/videos/{video_id}/frames")
    assert frames_response.status_code == 200
    assert frames_response.json()["sampling"]["method"] == "scene_change"
