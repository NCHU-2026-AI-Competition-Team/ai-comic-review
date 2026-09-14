"""视频上传与处理状态查询路由。"""

import json
import logging
import re
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import ROOT_DIR, get_settings
from app.core.ids import InvalidVideoIdError, normalize_video_id

# uvicorn 从 backend/ 启动时仓库根不在 sys.path；抽象异常定义在 ai.asr.base / ai.vlm.base
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ai.asr.base import (  # noqa: E402
    AsrNotConfiguredError,
    AsrResponseFormatError,
    AsrServiceError,
    AsrTimeoutError,
)
from ai.vlm.base import (  # noqa: E402
    VlmNotConfiguredError,
    VlmResponseFormatError,
    VlmServiceError,
    VlmTimeoutError,
)
from app.schemas.events import (
    EventModality,
    ModalityEventsFile,
    ModalityRunResponse,
    ReviewRunResponse,
    TimelineEvent,
)
from app.schemas.report import ReviewReport
from app.schemas.video import FramesInfo, SamplingMode, VideoJob, VideoUploadResponse
from app.services import asr_pipeline, audio, ocr_pipeline, registry, vlm_pipeline
from app.services.modality_store import modality_result_path
from app.services.storage_paths import UnsafePathError, resolve_in_dir
from app.services.uploads import ALLOWED_VIDEO_EXTENSIONS, UploadNotFoundError, find_uploaded_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/videos", tags=["videos"])

ALLOWED_EXTENSIONS = set(ALLOWED_VIDEO_EXTENSIONS)
ALLOWED_FRAME_SUFFIXES = {".jpg", ".jpeg", ".png"}
ALLOWED_SAMPLING_MODES = {"fixed_fps", "scene"}

# 视频流响应的 MIME 按文件后缀映射，缺失时回退通用二进制类型
VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}


def _validate_video_id(video_id: str) -> str:
    """校验 video_id 为合法 UUID，防止路径穿越；非法格式返回 400。"""
    try:
        return normalize_video_id(video_id)
    except InvalidVideoIdError:
        raise HTTPException(status_code=400, detail="video_id 格式非法") from None


def _validate_sampling(sampling: str) -> SamplingMode:
    """校验采样模式取值，非法值返回 400。"""
    if sampling not in ALLOWED_SAMPLING_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的采样模式 '{sampling}'，仅允许 {sorted(ALLOWED_SAMPLING_MODES)}",
        )
    return sampling  # type: ignore[return-value]


def _try_process(video_id: str, sampling: SamplingMode) -> None:
    """尝试触发视频处理流水线。

    app.services.video.process_video 由视频处理服务提供，
    缺失（ImportError）时保持 processing 状态；其他异常记为 failed。
    """
    try:
        from app.services.video import process_video
    except ImportError:
        logger.info("视频处理模块尚未接入，video_id=%s 保持 processing", video_id)
        return
    try:
        process_video(video_id, sampling=sampling)
    except Exception as exc:
        logger.exception("视频处理失败 video_id=%s", video_id)
        registry.update_job(video_id, status="failed", error=str(exc))
        raise


def _save_upload(file: UploadFile, dest: Path, max_bytes: int) -> None:
    """流式保存上传文件，超过大小限制时清理部分文件并返回 413。"""
    size = 0
    exceeded = False
    with dest.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                exceeded = True
                break
            out.write(chunk)
    if exceeded:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="上传文件超过大小限制")


@router.post("", response_model=VideoUploadResponse, status_code=201)
def upload_video(file: UploadFile, sampling: str = Form("fixed_fps")) -> VideoUploadResponse:
    """上传视频：校验格式、保存原始文件、落盘任务记录并尝试触发处理。"""
    original_name = file.filename or ""
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的视频格式 '{ext}'，仅允许 {sorted(ALLOWED_EXTENSIONS)}",
        )
    sampling_mode = _validate_sampling(sampling)

    settings = get_settings()
    video_id = str(uuid.uuid4())
    dest = resolve_in_dir(settings.uploads_path, f"{video_id}{ext}")
    settings.uploads_path.mkdir(parents=True, exist_ok=True)
    _save_upload(file, dest, max_bytes=settings.max_upload_size_mb * 1024 * 1024)

    job = registry.save_job(VideoJob(video_id=video_id, filename=original_name, sampling=sampling_mode))

    try:
        _try_process(video_id, sampling_mode)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"视频处理失败: {exc}") from exc

    job = registry.get_job(video_id) or job
    return VideoUploadResponse(
        video_id=job.video_id,
        filename=job.filename,
        status=job.status,
        sampling=job.sampling,
        metadata=job.metadata,
        frames=job.frames,
    )


@router.get("/{video_id}", response_model=VideoJob)
def get_video(video_id: str) -> VideoJob:
    """查询视频任务的当前状态与已有结果。"""
    video_id = _validate_video_id(video_id)
    try:
        job = registry.get_job(video_id)
    except registry.JobCorruptedError as exc:
        raise HTTPException(status_code=500, detail="任务记录文件损坏") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    return job


def _video_media_type(video_path: Path) -> str:
    """按文件后缀返回视频 MIME 类型。"""
    return VIDEO_MEDIA_TYPES.get(video_path.suffix.lower(), "application/octet-stream")


def _range_not_satisfiable(file_size: int) -> HTTPException:
    """构造 416 响应，统一携带 Content-Range: bytes */{file_size}。"""
    return HTTPException(
        status_code=416,
        detail="Requested Range Not Satisfiable",
        headers={"Content-Range": f"bytes */{file_size}"},
    )


def _parse_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    """将 Range 请求头解析为 (start, end) 闭区间。

    仅接受 bytes=start-end / bytes=start- / bytes=-suffix 三类单区间；
    多区间或无法识别的畸形值返回 None（忽略 Range，回退 200 全量）；
    合法但不可满足的区间抛 416。end 超出文件大小时裁剪到 file_size-1。
    """
    value = range_header.strip()
    if not value.startswith("bytes="):
        return None
    spec = value[len("bytes="):]
    if "," in spec:
        # 多区间暂不实现 multipart/byteranges，按 RFC 允许的方式忽略 Range
        return None
    match = re.fullmatch(r"(\d*)-(\d*)", spec)
    if match is None or (not match.group(1) and not match.group(2)):
        return None
    start_s, end_s = match.group(1), match.group(2)
    if file_size <= 0:
        raise _range_not_satisfiable(file_size)
    if not start_s:
        # bytes=-suffix：取文件末尾 suffix 字节
        suffix = int(end_s)
        if suffix <= 0:
            raise _range_not_satisfiable(file_size)
        return max(file_size - suffix, 0), file_size - 1
    start = int(start_s)
    end = int(end_s) if end_s else file_size - 1
    if end >= file_size:
        end = file_size - 1
    if start >= file_size or start > end:
        raise _range_not_satisfiable(file_size)
    return start, end


@router.get("/{video_id}/file")
def get_video_file(video_id: str, request: Request) -> Response:
    """流式返回原始视频，支持单区间 HTTP Range。"""
    video_id = _validate_video_id(video_id)
    try:
        video_path = find_uploaded_file(video_id)
    except UploadNotFoundError:
        raise HTTPException(status_code=404, detail="视频文件不存在") from None

    file_size = video_path.stat().st_size
    media_type = _video_media_type(video_path)
    range_header = request.headers.get("range")

    def file_iterator(path: Path, start_pos: int, chunk_sz: int):
        with open(path, "rb") as f:
            f.seek(start_pos)
            bytes_read = 0
            while bytes_read < chunk_sz:
                read_size = min(1024 * 1024, chunk_sz - bytes_read)
                data = f.read(read_size)
                if not data:
                    break
                bytes_read += len(data)
                yield data

    parsed = _parse_range(range_header, file_size) if range_header else None
    if parsed is None:
        if not range_header:
            return FileResponse(video_path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
        # 忽略无法支持的 Range（多区间/畸形值）返回 200 全量；
        # 不能走 FileResponse，否则 Starlette 会按 scope 里的 Range 头自行处理
        return StreamingResponse(
            file_iterator(video_path, 0, file_size),
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )

    start, end = parsed
    chunk_size = end - start + 1

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_size),
    }
    return StreamingResponse(
        file_iterator(video_path, start, chunk_size),
        status_code=206,
        media_type=media_type,
        headers=headers
    )


@router.get("/{video_id}/frames", response_model=FramesInfo)
def get_video_frames(video_id: str) -> FramesInfo:
    """读取抽帧结果，尚未生成时返回 404。"""
    video_id = _validate_video_id(video_id)
    frames_file = resolve_in_dir(get_settings().frames_path, video_id, "frames.json")
    if not frames_file.is_file():
        raise HTTPException(status_code=404, detail="抽帧结果尚未生成")
    try:
        data = json.loads(frames_file.read_text(encoding="utf-8"))
        return FramesInfo.model_validate(data)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("抽帧结果损坏 video_id=%s: %s", video_id, exc)
        raise HTTPException(status_code=500, detail="抽帧结果文件损坏") from exc


@router.get("/{video_id}/frames/{filename}")
def get_frame_image(video_id: str, filename: str) -> FileResponse:
    """按帧文件名访问帧图片。"""
    video_id = _validate_video_id(video_id)
    if Path(filename).name != filename or Path(filename).suffix.lower() not in ALLOWED_FRAME_SUFFIXES:
        raise HTTPException(status_code=404, detail="帧图片不存在")
    try:
        frame_file = resolve_in_dir(get_settings().frames_path, video_id, filename)
    except UnsafePathError:
        raise HTTPException(status_code=404, detail="帧图片不存在") from None
    if not frame_file.is_file():
        raise HTTPException(status_code=404, detail="帧图片不存在")
    return FileResponse(frame_file)


@router.post("/{video_id}/ocr", response_model=ModalityRunResponse)
def run_video_ocr(
    video_id: str,
    force: bool = Query(False, description="与 ASR 接口对齐的保留参数；OCR 当前总是重新识别"),
) -> ModalityRunResponse:
    """对已完成抽帧的视频同步执行 OCR，产出 ocr 模态时间线事件。"""
    video_id = _validate_video_id(video_id)
    try:
        job = registry.get_job(video_id)
    except registry.JobCorruptedError as exc:
        raise HTTPException(status_code=500, detail="任务记录文件损坏") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    try:
        events = ocr_pipeline.run_ocr(video_id)
    except ocr_pipeline.FramesNotFoundError as exc:
        raise HTTPException(status_code=409, detail="尚未完成抽帧，请先完成视频处理") from exc
    except ocr_pipeline.FramesCorruptedError as exc:
        raise HTTPException(status_code=500, detail="帧清单文件损坏") from exc
    return ModalityRunResponse(video_id=video_id, modality="ocr", event_count=len(events))


@router.post("/{video_id}/asr", response_model=ModalityRunResponse)
def run_video_asr(
    video_id: str,
    force: bool = Query(False, description="为 true 时忽略已有 asr.json 强制重跑"),
) -> ModalityRunResponse:
    """对视频同步执行 ASR（自动提取音频并调用云端识别），产出 asr 模态时间线事件。

    默认复用已有 asr.json 并标注 reused=true；force=true 才重新识别。
    """
    video_id = _validate_video_id(video_id)
    try:
        job = registry.get_job(video_id)
    except registry.JobCorruptedError as exc:
        raise HTTPException(status_code=500, detail="任务记录文件损坏") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    try:
        outcome = asr_pipeline.run_asr(video_id, force=force)
    except audio.UploadNotFoundError as exc:
        raise HTTPException(status_code=409, detail="找不到原始视频文件，请重新上传") from exc
    except audio.NoAudioTrackError as exc:
        raise HTTPException(status_code=409, detail="视频不含音轨，无法执行语音识别") from exc
    except audio.AudioExtractTimeoutError as exc:
        raise HTTPException(status_code=504, detail="音频提取超时") from exc
    except AsrTimeoutError as exc:
        raise HTTPException(status_code=504, detail="云端语音识别超时") from exc
    except AsrNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="未配置云端 ASR 服务端点") from exc
    except (AsrServiceError, AsrResponseFormatError) as exc:
        raise HTTPException(status_code=502, detail="云端语音识别服务异常") from exc
    except audio.InvalidAudioError as exc:
        raise HTTPException(status_code=500, detail="音频文件损坏或格式不符") from exc
    return ModalityRunResponse(
        video_id=video_id,
        modality="asr",
        event_count=len(outcome.events),
        reused=outcome.reused,
    )


@router.post("/{video_id}/review", response_model=ReviewRunResponse)
def run_video_review(
    video_id: str,
    force: bool = Query(False, description="为 true 时忽略已有 vlm.json 强制重跑"),
) -> ReviewRunResponse:
    """对已完成抽帧的视频同步执行 VLM 主审，产出 vlm 模态时间线事件。

    默认复用已有 vlm.json 并标注 reused=true；force=true 才重新审核。
    读取已落盘的 OCR/ASR 作为文本上下文（缺失则留空）。
    32B 复审未部署时保留 8B 结果并标注 needs_escalation / 待复审。
    """
    video_id = _validate_video_id(video_id)
    try:
        job = registry.get_job(video_id)
    except registry.JobCorruptedError as exc:
        raise HTTPException(status_code=500, detail="任务记录文件损坏") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    try:
        outcome = vlm_pipeline.run_review(video_id, force=force)
    except vlm_pipeline.FramesNotFoundError as exc:
        raise HTTPException(status_code=409, detail="尚未完成抽帧，请先完成视频处理") from exc
    except vlm_pipeline.FramesCorruptedError as exc:
        raise HTTPException(status_code=500, detail="帧清单文件损坏") from exc
    except VlmTimeoutError as exc:
        raise HTTPException(status_code=504, detail="云端视觉审核超时") from exc
    except VlmNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="未配置云端 VLM 服务端点") from exc
    except (VlmServiceError, VlmResponseFormatError) as exc:
        raise HTTPException(status_code=502, detail="云端视觉审核服务异常") from exc
    return ReviewRunResponse(
        video_id=video_id,
        modality="vlm",
        event_count=len(outcome.events),
        reused=outcome.reused,
        needs_escalation=outcome.needs_escalation,
        escalation_status=outcome.escalation_status,
    )


@router.get("/{video_id}/report", response_model=ReviewReport)
def get_video_report(video_id: str) -> ReviewReport:
    """读取结构化审核报告：视频元信息、各模态状态、风险事件与整体结论。"""
    video_id = _validate_video_id(video_id)
    try:
        job = registry.get_job(video_id)
    except registry.JobCorruptedError as exc:
        raise HTTPException(status_code=500, detail="任务记录文件损坏") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    events_file = modality_result_path(video_id, "vlm")
    if not events_file.is_file():
        raise HTTPException(status_code=404, detail="审核报告尚未生成")
    try:
        data = json.loads(events_file.read_text(encoding="utf-8"))
        events_payload = ModalityEventsFile.model_validate(data)
        if events_payload.video_id != video_id or events_payload.modality != "vlm":
            raise ValueError(
                f"事件文件与请求不一致：文件 video_id={events_payload.video_id} "
                f"modality={events_payload.modality}"
            )
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("审核结果损坏 video_id=%s: %s", video_id, exc)
        raise HTTPException(status_code=500, detail="审核结果文件损坏") from exc
    return vlm_pipeline.build_review_report(video_id, events_payload.events)


@router.get("/{video_id}/events", response_model=list[TimelineEvent])
def get_video_events(
    video_id: str,
    modality: EventModality = Query("ocr", description="事件来源模态"),
) -> list[TimelineEvent]:
    """读取已生成的模态事件列表，尚未生成时返回 404。"""
    video_id = _validate_video_id(video_id)
    events_file = modality_result_path(video_id, modality)
    if not events_file.is_file():
        raise HTTPException(status_code=404, detail="事件尚未生成")
    try:
        data = json.loads(events_file.read_text(encoding="utf-8"))
        events_payload = ModalityEventsFile.model_validate(data)
        if events_payload.video_id != video_id or events_payload.modality != modality:
            raise ValueError(
                f"事件文件与请求不一致：文件 video_id={events_payload.video_id} "
                f"modality={events_payload.modality}"
            )
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("事件文件损坏 video_id=%s modality=%s: %s", video_id, modality, exc)
        raise HTTPException(status_code=500, detail="事件文件损坏") from exc
    return events_payload.events
