export type VideoStatus = 'processing' | 'processed' | 'failed'

export type SamplingMode = 'fixed_fps' | 'scene'

export type SamplingMethod = 'fixed_fps' | 'scene_change'

// 与后端 SamplingInfo 的 method 字段联动校验一一对应：fps 与 threshold 互斥
export type SamplingInfo =
  | { method: 'fixed_fps'; fps: number; threshold: null }
  | { method: 'scene_change'; fps: null; threshold: number }

export interface VideoMetadata {
  duration: number | null
  width: number | null
  height: number | null
  fps: number | null
  codec: string | null
  bitrate: number | null
}

export interface FrameInfo {
  frame_id: string
  timestamp_ms: number
  timestamp: string
  path: string
}

export interface FramesInfo {
  sampling: SamplingInfo
  count: number
  frames: FrameInfo[]
}

export interface VideoJob {
  video_id: string
  filename: string
  status: VideoStatus
  sampling: SamplingMode
  error: string | null
  metadata: VideoMetadata | null
  frames: FramesInfo | null
  created_at: string
}

export interface VideoUploadResponse {
  video_id: string
  filename: string
  status: VideoStatus
  sampling: SamplingMode
  metadata: VideoMetadata | null
  frames: FramesInfo | null
}
