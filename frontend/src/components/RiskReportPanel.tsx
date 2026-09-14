import { useEffect, useState } from 'react'
import type { RiskReport, TimelineEvent } from '../types'
import { formatTimestamp } from './FramesGrid'
import { ApiError, getReport } from '../api/videos'

interface Props {
  videoId: string
  vlmEvents: TimelineEvent[]
}

const SEVERITY_LEVELS = ['high', 'medium', 'low', 'info'] as const
type Severity = (typeof SEVERITY_LEVELS)[number]

/** severity 转小写后按白名单映射，未知值降级为 info。 */
function normalizeSeverity(value: unknown): Severity {
  const lowered = String(value ?? '').toLowerCase()
  return (SEVERITY_LEVELS as readonly string[]).includes(lowered) ? (lowered as Severity) : 'info'
}

export default function RiskReportPanel({ videoId, vlmEvents }: Props) {
  const [report, setReport] = useState<RiskReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retryKey, setRetryKey] = useState(0)

  useEffect(() => {
    let canceled = false
    setError(null)

    if (!videoId) return

    getReport(videoId)
      .then((data) => {
        if (!canceled) {
          setReport(data)
        }
      })
      .catch((err) => {
        if (!canceled) {
          if (err instanceof ApiError && err.status === 404) {
            setReport(null) // 降级到 vlmEvents
          } else {
            setError(err instanceof Error ? err.message : '网络错误')
          }
        }
      })

    return () => {
      canceled = true
    }
  }, [videoId, vlmEvents, retryKey])

  if (error) {
    return (
      <div className="risk-report-panel panel">
        <h2>风险报告</h2>
        <div className="error-banner">
          <span>{error}</span>
          <button type="button" onClick={() => setRetryKey((k) => k + 1)}>
            重试
          </button>
        </div>
      </div>
    )
  }

  if (report) {
    const { overall, modalities, risk_events } = report
    const needsEscalation = overall?.needs_escalation || overall?.escalation_status === 'pending_review'

    return (
      <div className="risk-report-panel panel">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>风险报告</h2>
          {needsEscalation && (
            <span style={{ background: 'var(--error)', color: '#fff', padding: '2px 8px', borderRadius: 4, fontSize: 12, fontWeight: 'bold' }}>
              待复审
            </span>
          )}
        </div>

        {overall && (
          <div className="risk-item" style={{ marginBottom: 16 }}>
            <div className="risk-header">
              <span className={`risk-severity severity-${normalizeSeverity(overall.severity)}`}>
                {overall.risk || '总体风险'} ({normalizeSeverity(overall.severity).toUpperCase()})
              </span>
              {typeof overall.confidence === 'number' && (
                <span className="risk-time">置信度: {(overall.confidence * 100).toFixed(0)}%</span>
              )}
            </div>
            <div className="risk-category">{overall.category || '未分类'}</div>
            {overall.reason && <div className="risk-reason"><strong>原因：</strong>{overall.reason}</div>}
            {overall.suggestion && <div className="risk-suggestion"><strong>建议：</strong>{overall.suggestion}</div>}
            
            {modalities && (
              <div style={{ marginTop: 12, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {['ocr', 'asr', 'vlm'].map((mod) => {
                  const m = modalities[mod as keyof typeof modalities]
                  return (
                    <span key={mod} style={{ fontSize: 12, padding: '2px 6px', background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 4, color: m?.ran ? 'var(--text)' : 'var(--text-dim)' }}>
                      {mod.toUpperCase()}: {m?.ran ? `${m.event_count} 事件` : '未运行'}
                    </span>
                  )
                })}
              </div>
            )}
          </div>
        )}

        <h3 style={{ fontSize: 15, marginBottom: 8, marginTop: overall ? 16 : 0 }}>
          风险事件 ({risk_events?.length || 0})
        </h3>
        {!risk_events || risk_events.length === 0 ? (
          <div className="risk-empty">
            <p className="hint">无风险事件数据。</p>
          </div>
        ) : (
          <ul className="risk-list">
            {risk_events.map((ev, idx) => {
              const severity = normalizeSeverity(ev.severity)
              return (
                <li key={idx} className="risk-item">
                  <div className="risk-header">
                    <span className={`risk-severity severity-${severity}`}>
                      {severity.toUpperCase()}
                    </span>
                    <span className="risk-time">
                      {formatTimestamp(ev.start_ms)} - {formatTimestamp(ev.end_ms)}
                    </span>
                  </div>
                  <div className="risk-category">{ev.category || '未分类'}</div>
                  {ev.evidence && <div className="risk-content">{ev.evidence}</div>}
                  {ev.reason && <div className="risk-reason"><strong>原因：</strong>{ev.reason}</div>}
                  {ev.suggestion && <div className="risk-suggestion"><strong>建议：</strong>{ev.suggestion}</div>}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    )
  }

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
