"""镜头切换检测测试：showinfo 解析、真实合成视频边界检测、结构与上限校验。"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.video import FramesInfo, SamplingInfo, VideoJob, VideoMetadata
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


def test_parse_showinfo_pts_scientific_notation() -> None:
    """科学计数法 pts_time 必须完整解析，不得按普通浮点截断。"""
    sample = (
        "[Parsed_showinfo_1 @ 000001ab] n: 1 pts: 1 pts_time:1.23e+03 pos: 1 fmt:yuv420p\n"
        "[Parsed_showinfo_1 @ 000001ab] n: 2 pts: 2 pts_time:1e-03 pos: 2 fmt:yuv420p\n"
        "[Parsed_showinfo_1 @ 000001ab] n: 3 pts: 3 pts_time:-1.23e+03 pos: 3 fmt:yuv420p"
    )
    assert scene._parse_showinfo_pts(sample) == [-1230000, 1, 1230000]


def test_parse_showinfo_pts_edge_float_forms() -> None:
    """负值、.5、1. 等边界浮点形式均可解析为毫秒。"""
    sample = (
        "[Parsed_showinfo_1 @ 000001ab] n: 1 pts: 1 pts_time:-0.5 pos: 1 fmt:yuv420p\n"
        "[Parsed_showinfo_1 @ 000001ab] n: 2 pts: 2 pts_time:.5 pos: 2 fmt:yuv420p\n"
        "[Parsed_showinfo_1 @ 000001ab] n: 3 pts: 3 pts_time:1. pos: 3 fmt:yuv420p"
    )
    assert scene._parse_showinfo_pts(sample) == [-500, 500, 1000]


def test_parse_showinfo_pts_invalid_and_missing() -> None:
    """pts_time:N/A 与缺少 pts_time 的 showinfo 行不产生时间戳。"""
    sample = (
        "[Parsed_showinfo_1 @ 000001ab] n: 1 pts: 1 pts_time:N/A pos: 1 fmt:yuv420p\n"
        "[Parsed_showinfo_1 @ 000001ab] n: 2 pts: 2 pos: 2 fmt:yuv420p"
    )
    assert scene._parse_showinfo_pts(sample) == []


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

    monkeypatch.setattr(
        scene, "detect_scene_changes",
        lambda path, threshold, max_boundaries=None: list(range(1000, 10000, 100)),
    )
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
def test_detect_scene_changes_respects_max_boundaries(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """检测阶段帧数上限：ffmpeg 输出达到上限即提前终止并告警，结果只含前 N 个边界。"""
    video_file = tmp_path / "scenes.mp4"
    _make_scene_test_video(video_file)

    with caplog.at_level(logging.WARNING, logger=scene.logger.name):
        boundaries = scene.detect_scene_changes(video_file, threshold=0.4, max_boundaries=1)

    assert len(boundaries) == 1
    assert abs(boundaries[0] - 2000) <= 500
    assert any("上限" in record.message for record in caplog.records)


def _jpeg_mean_brightness(frame_file: Path) -> float:
    """用 ffmpeg 把 JPEG 解码为灰度裸流并计算平均亮度（0-255），避免引入 PIL 依赖。"""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(frame_file), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True,
        capture_output=True,
    )
    data = result.stdout
    assert data, f"解码 {frame_file} 未得到像素数据"
    return sum(data) / len(data)


@requires_ffmpeg
def test_extract_scene_frames_boundary_colors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """边界帧内容校验：黑/白/黑分段视频的各帧平均亮度必须落在对应分段。

    精确 seek 的回归保障：若抽帧仍用快速 seek 产生帧偏移，
    边界帧会落到相邻分段导致亮度断言失败。
    """
    _isolate_storage(tmp_path, monkeypatch)
    video_file = tmp_path / "scenes.mp4"
    _make_scene_test_video(video_file)

    info = scene.extract_scene_frames(VIDEO_ID, video_file)

    assert info.count == 3
    out_dir = tmp_path / "frames" / VIDEO_ID
    brightness = [_jpeg_mean_brightness(out_dir / f"{f.frame_id}.jpg") for f in info.frames]
    # 分段为黑/白/黑：起始帧与末边界帧为黑，中间边界帧为白
    assert brightness[0] < 64
    assert brightness[1] > 192
    assert brightness[2] < 64


@requires_ffmpeg
def test_extract_scene_frames_no_scene_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无镜头切换的纯色视频只产出起始帧。"""
    _isolate_storage(tmp_path, monkeypatch)
    video_file = tmp_path / "static.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=gray:size=320x240:rate=10:duration=2",
            "-pix_fmt", "yuv420p",
            str(video_file),
        ],
        check=True,
        capture_output=True,
    )

    info = scene.extract_scene_frames(VIDEO_ID, video_file)

    assert info.count == 1
    assert info.frames[0].timestamp_ms == 0
    assert (tmp_path / "frames" / VIDEO_ID / f"{info.frames[0].frame_id}.jpg").is_file()


@requires_ffmpeg
def test_extract_scene_frames_tiny_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """极短视频（不足 1 秒）不报错，至少产出起始帧。"""
    _isolate_storage(tmp_path, monkeypatch)
    video_file = tmp_path / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=black:size=320x240:rate=10:duration=0.2",
            "-pix_fmt", "yuv420p",
            str(video_file),
        ],
        check=True,
        capture_output=True,
    )

    info = scene.extract_scene_frames(VIDEO_ID, video_file)

    assert info.count >= 1
    assert info.frames[0].timestamp_ms == 0


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


def test_extract_scene_frames_uses_explicit_threshold_and_max_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式传入 threshold / max_frames 时覆盖全局配置，并写入 frames.json。"""
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("SCENE_THRESHOLD", "0.4")
    monkeypatch.setenv("SCENE_MAX_FRAMES", "500")
    from app.core.config import get_settings

    get_settings.cache_clear()

    captured: dict[str, object] = {}

    def fake_detect(path: Path, threshold: float, max_boundaries=None):
        captured["threshold"] = threshold
        captured["max_boundaries"] = max_boundaries
        return list(range(1000, 5000, 1000))

    monkeypatch.setattr(scene, "detect_scene_changes", fake_detect)
    monkeypatch.setattr(scene, "_require_tool", lambda name: name)
    monkeypatch.setattr(
        scene,
        "_extract_single_frame",
        lambda ffmpeg, video_path, timestamp_ms, dest: dest.write_bytes(b"\xff\xd8\xff"),
    )

    info = scene.extract_scene_frames(
        VIDEO_ID, Path("dummy.mp4"), threshold=0.15, max_frames=3
    )
    assert captured["threshold"] == pytest.approx(0.15)
    assert captured["max_boundaries"] == 2
    assert info.count == 3
    assert info.sampling.threshold == pytest.approx(0.15)


def test_process_video_uses_job_scene_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """process_video 将任务级 scene_threshold / scene_max_frames 传给镜头抽帧。"""
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("SCENE_THRESHOLD", "0.4")
    monkeypatch.setenv("SCENE_MAX_FRAMES", "500")
    from app.core.config import get_settings

    get_settings.cache_clear()
    uploads = get_settings().uploads_path
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / f"{VIDEO_ID}.mp4").write_bytes(b"fake-mp4")
    registry.save_job(
        VideoJob(
            video_id=VIDEO_ID,
            filename="scenes.mp4",
            sampling="scene",
            scene_threshold=0.2,
            scene_max_frames=7,
        )
    )

    captured: dict[str, object] = {}
    monkeypatch.setattr(video, "probe_metadata", lambda path: VideoMetadata())

    def fake_extract(video_id: str, video_path: Path, threshold=None, max_frames=None):
        captured["threshold"] = threshold
        captured["max_frames"] = max_frames
        return FramesInfo(
            sampling=SamplingInfo(method="scene_change", threshold=threshold),
            count=0,
            frames=[],
        )

    monkeypatch.setattr(scene, "extract_scene_frames", fake_extract)
    video.process_video(VIDEO_ID, sampling="scene")
    assert captured["threshold"] == pytest.approx(0.2)
    assert captured["max_frames"] == 7


def test_process_video_scene_falls_back_to_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未提供镜头覆盖参数时 process_video 使用全局 SCENE_* 配置。"""
    _isolate_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("SCENE_THRESHOLD", "0.35")
    monkeypatch.setenv("SCENE_MAX_FRAMES", "42")
    from app.core.config import get_settings

    get_settings.cache_clear()
    uploads = get_settings().uploads_path
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / f"{VIDEO_ID}.mp4").write_bytes(b"fake-mp4")
    registry.save_job(VideoJob(video_id=VIDEO_ID, filename="scenes.mp4", sampling="scene"))

    captured: dict[str, object] = {}
    monkeypatch.setattr(video, "probe_metadata", lambda path: VideoMetadata())

    def fake_extract(video_id: str, video_path: Path, threshold=None, max_frames=None):
        captured["threshold"] = threshold
        captured["max_frames"] = max_frames
        return FramesInfo(
            sampling=SamplingInfo(method="scene_change", threshold=threshold),
            count=0,
            frames=[],
        )

    monkeypatch.setattr(scene, "extract_scene_frames", fake_extract)
    video.process_video(VIDEO_ID, sampling="scene")
    assert captured["threshold"] == pytest.approx(0.35)
    assert captured["max_frames"] == 42


@requires_ffmpeg
def test_upload_scene_overrides_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """API 级：scene_threshold / scene_max_frames 覆盖生效并回显。"""
    _isolate_storage(tmp_path, monkeypatch)
    video_file = tmp_path / "scenes.mp4"
    _make_scene_test_video(video_file)

    with video_file.open("rb") as f:
        response = client.post(
            "/api/videos",
            files={"file": ("scenes.mp4", f, "video/mp4")},
            data={
                "sampling": "scene",
                "scene_threshold": "0.3",
                "scene_max_frames": "1",
            },
        )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "processed"
    assert payload["frames"]["sampling"]["threshold"] == pytest.approx(0.3)
    assert payload["frames"]["count"] == 1

    video_id = payload["video_id"]
    job = registry.get_job(video_id)
    assert job is not None
    assert job.scene_threshold == pytest.approx(0.3)
    assert job.scene_max_frames == 1
    echoed = client.get(f"/api/videos/{video_id}").json()
    assert echoed["scene_threshold"] == pytest.approx(0.3)
    assert echoed["scene_max_frames"] == 1
