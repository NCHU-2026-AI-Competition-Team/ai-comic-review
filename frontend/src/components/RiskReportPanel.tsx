import type { TimelineEvent } from '../types'
import { formatTimestamp } from './FramesGrid'

interface Props {
  vlmEvents: TimelineEvent[]
}

export default function RiskReportPanel({ vlmEvents }: Props) {
  return (
    <div className="risk-report-panel panel">
      <h2>风险报告</h2>
      {vlmEvents.length === 0 ? (
        <div className="risk-empty">
          <p className="hint">暂无风险报告数据，请先运行 VLM 分析。</p>
        </div>
      ) : (
        <ul className="risk-list">
          {vlmEvents.map((ev) => {
            const m = ev.metadata || {}
            return (
              <li key={ev.id} className="risk-item">
                <div className="risk-header">
                  <span className={`risk-severity severity-${m.severity || 'info'}`}>
                    {String(m.severity || 'INFO').toUpperCase()}
                  </span>
                  <span className="risk-time">
                    {formatTimestamp(ev.start_ms)} - {formatTimestamp(ev.end_ms)}
                  </span>
                </div>
                <div className="risk-category">{String(m.category || '未分类')}</div>
                <div className="risk-content">{ev.content}</div>
                {Boolean(m.reason) && <div className="risk-reason"><strong>原因：</strong>{String(m.reason)}</div>}
                {Boolean(m.suggestion) && <div className="risk-suggestion"><strong>建议：</strong>{String(m.suggestion)}</div>}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
