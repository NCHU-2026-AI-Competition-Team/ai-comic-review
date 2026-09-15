import type {
  EventModality,
  FramesInfo,
  ModalityRunResponse,
  RiskReport,
  SamplingMode,
  TimelineEvent,
  VideoJob,
  VideoUploadResponse,
  VideoSummary,
} from '../types'

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? ''

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function parseError(res: Response): Promise<ApiError> {
  let detail = `HTTP ${res.status}`
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') {
      detail = body.detail
    }
  } catch {
    // 保留默认错误信息
  }
  return new ApiError(res.status, detail)
}

export async function uploadVideo(
  file: File,
  options: {
    sampling?: SamplingMode
    frame_fps?: number
    scene_threshold?: number
    scene_max_frames?: number
  } = {}
): Promise<VideoUploadResponse> {
  const form = new FormData()
  form.append('file', file, file.name)
  form.append('sampling', options.sampling || 'fixed_fps')
  
  if (options.frame_fps !== undefined) {
    form.append('frame_fps', options.frame_fps.toString())
  }
  if (options.scene_threshold !== undefined) {
    form.append('scene_threshold', options.scene_threshold.toString())
  }
  if (options.scene_max_frames !== undefined) {
    form.append('scene_max_frames', options.scene_max_frames.toString())
  }

  const res = await fetch(`${BASE_URL}/api/videos`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as VideoUploadResponse
}

export async function getVideo(videoId: string): Promise<VideoJob> {
  const res = await fetch(`${BASE_URL}/api/videos/${encodeURIComponent(videoId)}`)
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as VideoJob
}

export async function getFrames(videoId: string): Promise<FramesInfo> {
  const res = await fetch(`${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/frames`)
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as FramesInfo
}

/** 对已完成抽帧的视频同步执行 OCR，返回产出的事件数量。 */
export async function runOcr(videoId: string, force = false): Promise<ModalityRunResponse> {
  const res = await fetch(`${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/ocr${force ? '?force=true' : ''}`, {
    method: 'POST',
  })
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as ModalityRunResponse
}

/** 对视频同步执行 ASR（自动提取音频），返回产出的事件数量。 */
export async function runAsr(videoId: string, force = false): Promise<ModalityRunResponse> {
  const res = await fetch(`${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/asr${force ? '?force=true' : ''}`, {
    method: 'POST',
  })
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as ModalityRunResponse
}

/** 读取已生成的模态事件列表（ocr / asr / vlm 等）。 */
export async function getEvents(videoId: string, modality: EventModality = 'ocr'): Promise<TimelineEvent[]> {
  const res = await fetch(
    `${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/events?modality=${encodeURIComponent(modality)}`,
  )
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as TimelineEvent[]
}

/** 帧图片地址：frames.json 中的 path 即以 /api/videos/ 开头的可直接访问路径。 */
export function frameImageUrl(videoId: string, frame: { frame_id: string; path: string }): string {
  if (frame.path.startsWith('/api/')) {
    return `${BASE_URL}${frame.path}`
  }
  // 兼容旧版 frames.json（path 不带 /api 前缀）：按文件名拼接完整路由
  const filename = frame.path.split('/').pop() ?? `${frame.frame_id}.jpg`
  return `${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/frames/${encodeURIComponent(filename)}`
}

/** 读取视频审核的结构化报告 */
export async function getReport(videoId: string): Promise<RiskReport> {
  const res = await fetch(`${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/report`)
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as RiskReport
}

/** 触发全片风险审核 */
export async function runReview(videoId: string): Promise<RiskReport> {
  const res = await fetch(`${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/review`, {
    method: 'POST',
  })
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as RiskReport
}

/** 提交人工复核结果 */
export async function submitVerdict(
  videoId: string,
  verdict: { decision: 'approve' | 'reject' | 'false_positive'; note: string; event_id: string | null }
): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/verdict`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(verdict),
  })
  if (!res.ok) {
    throw await parseError(res)
  }
}

export async function getVideos(): Promise<VideoSummary[]> {
  const res = await fetch(`${BASE_URL}/api/videos`)
  if (!res.ok) {
    throw await parseError(res)
  }
  return (await res.json()) as VideoSummary[]
}
