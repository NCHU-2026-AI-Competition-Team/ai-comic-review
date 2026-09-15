import type { TimelineEvent } from '../types'

interface Props {
  duration: number | null
  currentTimeMs: number
  events: {
    ocr: TimelineEvent[]
    asr: TimelineEvent[]
    vlm: TimelineEvent[]
  }
  onSeek: (ms: number) => void
}

export default function TimelineSwimlanes({ duration, currentTimeMs, events, onSeek }: Props) {
  const allEvents = [...events.ocr, ...events.asr, ...events.vlm]
  // duration 缺失或为 0 时，以事件最大 end_ms 作为临时轴长并提示「时长未知」
  const durationMs = duration && duration > 0 ? duration * 1000 : null
  const maxEventEndMs = allEvents.reduce((max, ev) => Math.max(max, ev.end_ms), 0)
  const totalMs = durationMs ?? Math.max(maxEventEndMs, 1) // 兜底 1ms 避免除以 0

  const renderSwimlane = (title: string, modalityEvents: TimelineEvent[], color: string, emptyText?: string) => {
    return (
      <div className="swimlane">
        <div className="swimlane-label">{title}</div>
        <div className="swimlane-track" onClick={(e) => {
           // 点击轨道背景也能 seek（可选，这里实现为点击轨道根据比例 seek）
           const rect = e.currentTarget.getBoundingClientRect()
           const ratio = (e.clientX - rect.left) / rect.width
           onSeek(ratio * totalMs)
        }}>
          {modalityEvents.length === 0 && emptyText && (
            <div className="swimlane-empty">{emptyText}</div>
          )}
          {modalityEvents.map((ev) => {
            // 渲染前将事件区间钳制到 [0, totalMs]，且保证 end >= start
            const startMs = Math.min(Math.max(ev.start_ms, 0), totalMs)
            const endMs = Math.min(Math.max(ev.end_ms, startMs), totalMs)
            const leftPercent = (startMs / totalMs) * 100
            let widthPercent = ((endMs - startMs) / totalMs) * 100
            // 如果宽度太小（例如单帧的 OCR 事件），给一个最小宽度
            if (widthPercent < 0.5) widthPercent = 0.5
            if (leftPercent + widthPercent > 100) widthPercent = 100 - leftPercent

            return (
              <div
                key={ev.id}
                className="swimlane-event"
                style={{ left: `${leftPercent}%`, width: `${widthPercent}%`, backgroundColor: color }}
                title={ev.content}
                onClick={(e) => {
                  e.stopPropagation()
                  onSeek(ev.start_ms)
                }}
              />
            )
          })}
        </div>
      </div>
    )
  }

  const ratio = Math.min(1, Math.max(0, currentTimeMs / totalMs))
  const cursorLeft = `calc(98px + (100% - 114px) * ${ratio})`

  return (
    <div className="timeline-swimlanes" style={{ borderTop: '1px solid var(--border)' }}>
      <div className="swimlanes-container" style={{ borderRadius: '0 0 var(--radius-lg) var(--radius-lg)', border: 'none', boxShadow: 'none' }}>
        <div className="swimlanes-cursor" style={{ left: cursorLeft }} />
        {renderSwimlane('OCR', events.ocr, 'var(--accent)', '尚未运行 OCR')}
        {renderSwimlane('ASR', events.asr, '#4caf50', '尚未运行 ASR')}
        {renderSwimlane('VLM', events.vlm, '#e91e63', '暂无多模态分析')}
      </div>
    </div>
  )
}
