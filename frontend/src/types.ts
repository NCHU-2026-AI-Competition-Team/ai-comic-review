export type VideoStatus = 'processing' | 'processed' | 'failed'

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
  count: number
  frames: FrameInfo[]
}

export interface VideoJob {
  video_id: string
  filename: string
  status: VideoStatus
  error: string | null
  metadata: VideoMetadata | null
  frames: FramesInfo | null
  created_at: string
}

export interface VideoUploadResponse {
  video_id: string
  filename: string
  status: VideoStatus
  metadata: VideoMetadata | null
  frames: FramesInfo | null
}
