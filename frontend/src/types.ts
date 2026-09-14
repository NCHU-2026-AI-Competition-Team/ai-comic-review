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
  frame_fps?: number
  scene_threshold?: number
  scene_max_frames?: number
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

// 事件来源模态：与后端 backend/app/schemas/events.py 的 EventModality 保持一致（手工同步，不引入代码生成）
export type EventModality = 'ocr' | 'asr' | 'vision' | 'vlm'

export interface TimelineEvent {
  id: string
  video_id: string
  modality: EventModality
  start_ms: number
  end_ms: number
  content: string
  confidence: number
  metadata: Record<string, unknown>
}

export interface ModalityRunResponse {
  video_id: string
  modality: EventModality
  event_count: number
  reused?: boolean
}

export interface RiskEvent {
  start_ms: number
  end_ms: number
  category: string
  severity: string
  confidence: number
  evidence: string
  reason: string
  suggestion: string
  needs_escalation: boolean
}

export interface RiskReportOverall {
  risk: string
  category: string
  severity: string
  confidence: number
  suggestion: string
  reason: string
  needs_escalation: boolean
  escalation_status: 'not_needed' | 'pending_review' | string
}

export interface RiskReport {
  video_id: string
  filename: string
  status: string
  sampling: string
  metadata: VideoMetadata
  modalities: {
    ocr: { ran: boolean; event_count: number }
    asr: { ran: boolean; event_count: number }
    vlm: { ran: boolean; event_count: number }
  }
  risk_events: RiskEvent[]
  overall: RiskReportOverall
  verdict?: {
    decision: 'approve' | 'reject' | 'false_positive'
    note: string
    event_id: string | null
  } | null
}
