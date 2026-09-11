import type { FramesInfo, VideoJob, VideoUploadResponse } from '../types'

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

export async function uploadVideo(file: File): Promise<VideoUploadResponse> {
  const form = new FormData()
  form.append('file', file, file.name)

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

/** 帧图片地址：对应 GET /api/videos/{video_id}/frames/{filename} 路由。 */
export function frameImageUrl(videoId: string, frame: { frame_id: string; path: string }): string {
  const filename = frame.path.split('/').pop() ?? `${frame.frame_id}.jpg`
  return `${BASE_URL}/api/videos/${encodeURIComponent(videoId)}/frames/${encodeURIComponent(filename)}`
}
