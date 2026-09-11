import type { ReviewResult, UploadPayload } from './types'

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? ''

export async function submitReview(payload: UploadPayload): Promise<{ review_id: string }> {
  const form = new FormData()
  form.append('title', payload.title)
  form.append('genre', payload.genre)
  form.append('author_note', payload.authorNote)
  for (const file of payload.files) {
    form.append('pages', file, file.name)
  }

  const res = await fetch(`${BASE_URL}/api/reviews`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    throw new Error(`提交失败（HTTP ${res.status}）`)
  }
  return (await res.json()) as { review_id: string }
}

export async function fetchReview(reviewId: string): Promise<ReviewResult> {
  const res = await fetch(`${BASE_URL}/api/reviews/${encodeURIComponent(reviewId)}`)
  if (!res.ok) {
    throw new Error(`查询评审结果失败（HTTP ${res.status}）`)
  }
  return (await res.json()) as ReviewResult
}
