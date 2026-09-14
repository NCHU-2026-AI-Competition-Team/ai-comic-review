import type { TimelineEvent } from '../types'

interface Props {
  duration: number | null
  currentTimeMs: number
  events: {
    ocr: TimelineEvent[]
    asr: TimelineEvent[]
    vision: TimelineEvent[]
    vlm: TimelineEvent[]
  }
  onSeek: (ms: number) => void
}

export default function TimelineSwimlanes({ duration, currentTimeMs, events, onSeek }: Props) {
  const totalMs = duration ? duration * 1000 : 1 // 避免除以 0

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
            let leftPercent = (ev.start_ms / totalMs) * 100
            let widthPercent = ((ev.end_ms - ev.start_ms) / totalMs) * 100
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

  const cursorLeft = `${Math.min(100, Math.max(0, (currentTimeMs / totalMs) * 100))}%`

  return (
    <div className="timeline-swimlanes panel">
      <h2>时间轴</h2>
      <div className="swimlanes-container">
        <div className="swimlanes-cursor" style={{ left: cursorLeft }} />
        {renderSwimlane('OCR', events.ocr, 'var(--accent)')}
        {renderSwimlane('ASR', events.asr, '#4caf50')}
        {renderSwimlane('Vision', events.vision, '#ff9800', '暂无视觉事件')}
        {renderSwimlane('VLM', events.vlm, '#e91e63', '暂无多模态分析')}
      </div>
    </div>
  )
}
