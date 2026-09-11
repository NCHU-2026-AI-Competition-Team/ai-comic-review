export type ReviewStatus = 'pending' | 'processing' | 'completed' | 'failed'

export interface DimensionScore {
  name: string
  score: number
  max_score: number
  comment: string
}

export type IssueSeverity = 'info' | 'warning' | 'error'

export interface ReviewIssue {
  page: number
  severity: IssueSeverity
  category: string
  description: string
  suggestion: string
}

export interface ReviewResult {
  review_id: string
  title: string
  status: ReviewStatus
  overall_score: number | null
  summary: string
  dimensions: DimensionScore[]
  issues: ReviewIssue[]
  created_at: string
  finished_at: string | null
}

export interface UploadPayload {
  title: string
  genre: string
  authorNote: string
  files: File[]
}
