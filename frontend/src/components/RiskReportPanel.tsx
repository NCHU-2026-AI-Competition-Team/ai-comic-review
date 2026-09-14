import type { TimelineEvent } from '../types'
import { formatTimestamp } from './FramesGrid'

interface Props {
  vlmEvents: TimelineEvent[]
}

const SEVERITY_LEVELS = ['high', 'medium', 'low', 'info'] as const
type Severity = (typeof SEVERITY_LEVELS)[number]

/** severity 转小写后按白名单映射，未知值降级为 info。 */
function normalizeSeverity(value: unknown): Severity {
  const lowered = String(value ?? '').toLowerCase()
  return (SEVERITY_LEVELS as readonly string[]).includes(lowered) ? (lowered as Severity) : 'info'
}

export default function RiskReportPanel({ vlmEvents }: Props) {
  return (
    <div className="risk-report-panel panel">
      <h2>风险报告</h2>
      {vlmEvents.length === 0 ? (
        <div className="risk-empty">
          <p className="hint">暂无风险报告数据。</p>
        </div>
      ) : (
        <ul className="risk-list">
          {vlmEvents.map((ev) => {
            const m = ev.metadata || {}
            const severity = normalizeSeverity(m.severity)
            return (
              <li key={ev.id} className="risk-item">
                <div className="risk-header">
                  <span className={`risk-severity severity-${severity}`}>
                    {severity.toUpperCase()}
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
