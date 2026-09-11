import { useCallback, useEffect, useRef, useState } from 'react'
import UploadPanel from './components/UploadPanel'
import ResultPanel from './components/ResultPanel'
import { fetchReview, submitReview } from './api'
import { buildMockResult } from './mock'
import type { ReviewResult, UploadPayload } from './types'

type Phase = 'idle' | 'submitting' | 'reviewing' | 'done' | 'error'

const POLL_INTERVAL_MS = 2000

export default function App() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [result, setResult] = useState<ReviewResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lastTitle, setLastTitle] = useState('')
  const cancelRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    return () => cancelRef.current?.()
  }, [])

  const startPolling = useCallback((reviewId: string) => {
    let cancelled = false
    cancelRef.current = () => {
      cancelled = true
    }
    const tick = async () => {
      while (!cancelled) {
        try {
          const current = await fetchReview(reviewId)
          if (cancelled) return
          setResult(current)
          if (current.status === 'completed' || current.status === 'failed') {
            setPhase('done')
            return
          }
        } catch (err) {
          if (cancelled) return
          setError(err instanceof Error ? err.message : '查询评审进度失败')
          setPhase('error')
          return
        }
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
      }
    }
    void tick()
  }, [])

  const handleSubmit = useCallback(
    async (payload: UploadPayload) => {
      setPhase('submitting')
      setError(null)
      setLastTitle(payload.title)
      try {
        const { review_id } = await submitReview(payload)
        setResult({
          review_id,
          title: payload.title,
          status: 'pending',
          overall_score: null,
          summary: '',
          dimensions: [],
          issues: [],
          created_at: new Date().toISOString(),
          finished_at: null,
        })
        setPhase('reviewing')
        startPolling(review_id)
      } catch (err) {
        setError(err instanceof Error ? err.message : '提交失败')
        setPhase('error')
      }
    },
    [startPolling],
  )

  const handleReset = useCallback(() => {
    cancelRef.current?.()
    cancelRef.current = null
    setResult(null)
    setError(null)
    setPhase('idle')
  }, [])

  const handleDemo = useCallback(() => {
    setError(null)
    setResult(buildMockResult(lastTitle))
    setPhase('done')
  }, [lastTitle])

  const showResult = result !== null && (phase === 'reviewing' || phase === 'done')

  return (
    <div className="app">
      <header className="app-header">
        <h1>AI 漫画评审</h1>
        <p>上传漫画页面，获取 AI 对画面、叙事、角色、对白与分镜的专业评审</p>
      </header>

      <main>
        {phase === 'error' && (
          <div className="error-banner">
            <span>{error}</span>
            <button type="button" onClick={handleReset}>
              返回上传
            </button>
          </div>
        )}

        {showResult ? (
          <ResultPanel result={result} onReset={handleReset} />
        ) : (
          <UploadPanel submitting={phase === 'submitting'} onSubmit={handleSubmit} />
        )}

        {phase === 'idle' && (
          <p className="demo-link">
            后端尚未就绪？
            <button type="button" onClick={handleDemo}>
              查看演示评审结果
            </button>
          </p>
        )}
      </main>

      <footer className="app-footer">
        <p>AI Comic Review · 评审结果仅供参考</p>
      </footer>
    </div>
  )
}
