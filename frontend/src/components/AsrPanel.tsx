import { useCallback, useEffect, useState } from 'react'
import type { TimelineEvent } from '../types'
import { ApiError, getEvents, runAsr } from '../api/videos'
import { formatTimestamp } from './FramesGrid'

interface Props {
  videoId: string
  onEvents?: (events: TimelineEvent[]) => void
}

/** ASR 面板：触发语音识别并展示 asr 模态时间线事件（时间段 + 文本 + 置信度）。 */
export default function AsrPanel({ videoId, onEvents }: Props) {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reused, setReused] = useState(false)
  const [recovered, setRecovered] = useState(false)
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    if (!running) {
      setElapsed(0)
      return
    }
    const timer = setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => clearInterval(timer)
  }, [running])

  const hasResult = events !== null

  const handleRun = useCallback(
    async (force: boolean) => {
      setRunning(true)
      setError(null)
      setReused(false)
      setRecovered(false)
      // 重跑期间保留旧结果，仅在新结果成功返回后替换
      try {
        const res = await runAsr(videoId, force)
        setReused(res.reused ?? false)
        const newEvents = await getEvents(videoId, 'asr')
        setEvents(newEvents)
        if (onEvents) onEvents(newEvents)
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          setError(err.message)
        } else {
          try {
            const newEvents = await getEvents(videoId, 'asr')
            setEvents(newEvents)
            if (onEvents) onEvents(newEvents)
            setRecovered(true)
          } catch (fallbackErr) {
            setError(err instanceof Error ? err.message : 'ASR 识别失败')
          }
        }
      } finally {
        setRunning(false)
      }
    },
    [videoId, onEvents],
  )

  return (
    <div className="modality-panel">
      <h3>语音识别（ASR）</h3>
      <button className="primary" type="button" disabled={running} onClick={() => void handleRun(hasResult)}>
        {running ? (hasResult ? `正在重跑 ASR…… 已等待 ${elapsed}s` : `ASR 识别中…… 已等待 ${elapsed}s`) : hasResult ? '重跑 ASR' : '运行 ASR'}
      </button>
      {running && hasResult && <p className="hint">正在重跑，旧结果暂时保留</p>}
      {reused && <p className="hint">已复用上次成功结果</p>}
      {recovered && <p className="hint" style={{ color: '#4caf50' }}>已从服务端恢复结果</p>}
      {error && <p className="form-error">{error}</p>}
      {events && events.length === 0 && <p className="hint">未识别到任何语音</p>}
      {events && events.length > 0 && (
        <ul className="event-list">
          {events.map((event) => (
            <li key={event.id} className="event-item">
              <div className="event-header">
                <span className="event-timestamp">
                  {formatTimestamp(event.start_ms)} - {formatTimestamp(event.end_ms)}
                </span>
                <span className="event-confidence">{(event.confidence * 100).toFixed(1)}%</span>
              </div>
              <div className="event-content">{event.content}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
