import type { FramesInfo } from '../types'
import { frameImageUrl } from '../api/videos'

interface Props {
  videoId: string
  framesInfo: FramesInfo
}

/** 毫秒时间戳转 HH:MM:SS.mmm，例如 32500 → 00:00:32.500 */
export function formatTimestamp(timestampMs: number): string {
  const ms = timestampMs % 1000
  const totalSeconds = Math.floor(timestampMs / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (n: number, len = 2) => String(n).padStart(len, '0')
  return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}.${pad(ms, 3)}`
}

export default function FramesGrid({ videoId, framesInfo }: Props) {
  if (framesInfo.count === 0) {
    return <p className="hint">未抽取到任何帧</p>
  }
  return (
    <div className="frames">
      <h3>抽取帧（{framesInfo.count}）</h3>
      <ul className="frames-grid">
        {framesInfo.frames.map((frame) => (
          <li key={frame.frame_id} className="frame-item">
            <img
              src={frameImageUrl(videoId, frame)}
              alt={`第 ${formatTimestamp(frame.timestamp_ms)} 帧`}
              loading="lazy"
            />
            <span className="frame-timestamp">{formatTimestamp(frame.timestamp_ms)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
