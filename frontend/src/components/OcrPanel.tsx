import { useCallback, useState } from 'react'
import type { TimelineEvent } from '../types'
import { ApiError, getEvents, runOcr } from '../api/videos'
import { formatTimestamp } from './FramesGrid'

interface Props {
  videoId: string
  onEvents?: (events: TimelineEvent[]) => void
}

/** OCR 面板：触发识别并展示 ocr 模态时间线事件（时间戳 + 文本 + 置信度）。 */
export default function OcrPanel({ videoId, onEvents }: Props) {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reused, setReused] = useState(false)

  const handleRun = useCallback(async () => {
    setRunning(true)
    setError(null)
    setEvents(null)
    setReused(false)
    if (onEvents) onEvents([])
    try {
      const res = await runOcr(videoId)
      setReused(res.reused ?? false)
      const newEvents = await getEvents(videoId, 'ocr')
      setEvents(newEvents)
      if (onEvents) onEvents(newEvents)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError('视频尚未完成抽帧，请先完成视频处理')
      } else {
        setError(err instanceof Error ? err.message : 'OCR 执行失败')
      }
    } finally {
      setRunning(false)
    }
  }, [videoId, onEvents])

  return (
    <div className="modality-panel ocr-panel">
      <h3>文字识别（OCR）</h3>
      <button className="primary" type="button" disabled={running} onClick={handleRun}>
        {running ? 'OCR 识别中……' : '运行 OCR'}
      </button>
      {reused && <p className="hint">已复用上次成功结果</p>}
      {error && <p className="form-error">{error}</p>}
      {events && events.length === 0 && <p className="hint">未识别到任何文字</p>}
      {events && events.length > 0 && (
        <ul className="event-list">
          {events.map((event) => (
            <li key={event.id} className="event-item">
              <span className="event-timestamp">{formatTimestamp(event.start_ms)}</span>
              <span className="event-content">{event.content}</span>
              <span className="event-confidence">{(event.confidence * 100).toFixed(1)}%</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

