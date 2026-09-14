import { useCallback, useState } from 'react'
import type { TimelineEvent } from '../types'
import { ApiError, getEvents, runAsr } from '../api/videos'
import { formatTimestamp } from './FramesGrid'

interface Props {
  videoId: string
}

/** ASR 面板：触发语音识别并展示 asr 模态时间线事件（时间段 + 文本 + 置信度）。 */
export default function AsrPanel({ videoId }: Props) {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleRun = useCallback(async () => {
    setRunning(true)
    setError(null)
    try {
      await runAsr(videoId)
      setEvents(await getEvents(videoId, 'asr'))
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError(err.message)
      } else {
        setError(err instanceof Error ? err.message : 'ASR 识别失败')
      }
    } finally {
      setRunning(false)
    }
  }, [videoId])

  return (
    <div className="asr-panel">
      <h3>语音识别（ASR）</h3>
      <button className="primary" type="button" disabled={running} onClick={handleRun}>
        {running ? 'ASR 识别中……' : '运行 ASR'}
      </button>
      {error && <p className="form-error">{error}</p>}
      {events && events.length === 0 && <p className="hint">未识别到任何语音</p>}
      {events && events.length > 0 && (
        <ul className="event-list">
          {events.map((event) => (
            <li key={event.id} className="event-item">
              <span className="event-timestamp">
                {formatTimestamp(event.start_ms)} - {formatTimestamp(event.end_ms)}
              </span>
              <span className="event-content">{event.content}</span>
              <span className="event-confidence">{(event.confidence * 100).toFixed(1)}%</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
