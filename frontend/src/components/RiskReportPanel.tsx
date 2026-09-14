import { useEffect, useState } from 'react'
import type { RiskReport, TimelineEvent } from '../types'
import { formatTimestamp } from './FramesGrid'
import { ApiError, getReport, submitVerdict } from '../api/videos'

interface Props {
  videoId: string
  vlmEvents: TimelineEvent[]
  refreshKey?: number
  onSeek?: (ms: number) => void
}

const SEVERITY_LEVELS = ['high', 'medium', 'low', 'info'] as const
type Severity = (typeof SEVERITY_LEVELS)[number]

/** severity 转小写后按白名单映射，未知值降级为 info。 */
function normalizeSeverity(value: unknown): Severity {
  const lowered = String(value ?? '').toLowerCase()
  return (SEVERITY_LEVELS as readonly string[]).includes(lowered) ? (lowered as Severity) : 'info'
}

export default function RiskReportPanel({ videoId, vlmEvents, refreshKey, onSeek }: Props) {
  const [report, setReport] = useState<RiskReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retryKey, setRetryKey] = useState(0)

  const [verdictSubmitting, setVerdictSubmitting] = useState(false)
  const [verdictDecision, setVerdictDecision] = useState<'approve' | 'reject' | 'false_positive'>('approve')
  const [verdictNote, setVerdictNote] = useState('')

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
  }, [videoId, vlmEvents, retryKey, refreshKey])

  const handleVerdictSubmit = async () => {
    setVerdictSubmitting(true)
    setError(null)
    try {
      await submitVerdict(videoId, { decision: verdictDecision, note: verdictNote, event_id: null })
      const newReport = await getReport(videoId)
      setReport(newReport)
    } catch (err) {
      setError(err instanceof Error ? err.message : '提交复核失败')
    } finally {
      setVerdictSubmitting(false)
    }
  }

  const handleExportJson = () => {
    if (!report) return
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${videoId}-report.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const handlePrint = () => {
    window.print()
  }

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
    const { overall, modalities, risk_events, verdict } = report
    const needsEscalation = overall?.needs_escalation || overall?.escalation_status === 'pending_review'

    return (
      <div className="risk-report-panel panel">
        <style>{`
          @media print {
            .app-header, .upload-panel, .workbench-left, .workflow-panel, .modality-actions, .timeline-swimlanes, .frames-collapsible, button {
              display: none !important;
            }
            .workbench-main {
              display: block !important;
            }
            .workbench-right {
              width: 100% !important;
            }
            .risk-report-panel {
              box-shadow: none !important;
              border: none !important;
              max-height: none !important;
              overflow: visible !important;
            }
            .risk-report-panel::before {
              content: "视频文件: ${report.filename || videoId} | 生成时间: ${new Date().toLocaleString()}";
              display: block;
              margin-bottom: 20px;
              font-weight: bold;
              border-bottom: 1px solid #eee;
              padding-bottom: 10px;
            }
            .verdict-form, .export-actions {
              display: none !important;
            }
          }
        `}</style>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>风险报告</h2>
          {overall?.escalation_status ? (
            <span style={{ background: overall.escalation_status === 'pending_review' ? 'var(--error)' : 'var(--bg)', color: overall.escalation_status === 'pending_review' ? '#fff' : 'var(--text-dim)', padding: '2px 8px', borderRadius: 4, fontSize: 12, fontWeight: 'bold' }}>
              {overall.escalation_status === 'pending_review' ? '待复审' : 
               overall.escalation_status === 'not_needed' ? '无需复审' : 
               '已复审'}
            </span>
          ) : needsEscalation && (
            <span style={{ background: 'var(--error)', color: '#fff', padding: '2px 8px', borderRadius: 4, fontSize: 12, fontWeight: 'bold' }}>
              待复审
            </span>
          )}
        </div>

        {overall && (
          <div className="risk-item" style={{ marginBottom: 16 }}>
            <div className="risk-header">
              <span className={`risk-severity severity-${normalizeSeverity(overall.severity)}`}>
                {overall.risk ? '检出风险' : '未检出风险'} ({normalizeSeverity(overall.severity).toUpperCase()})
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

        {verdict && (
          <div className="risk-item verdict-item" style={{ marginBottom: 16, borderLeft: '4px solid #2196f3' }}>
            <div className="risk-header">
              <span className="risk-severity" style={{ background: '#2196f3', color: '#fff', border: 'none' }}>人工复核</span>
              <span className="risk-time" style={{ fontWeight: 'bold', color: verdict.decision === 'approve' ? '#4caf50' : verdict.decision === 'reject' ? '#f44336' : '#ff9800' }}>
                {verdict.decision === 'approve' ? '通过' : verdict.decision === 'reject' ? '打回' : '误报'}
              </span>
            </div>
            {verdict.note && <div className="risk-content">{verdict.note}</div>}
          </div>
        )}

        <div className="verdict-form panel" style={{ marginBottom: 16, padding: 12, background: 'var(--bg)' }}>
          <h3 style={{ margin: '0 0 8px 0', fontSize: 14 }}>复核操作</h3>
          <div style={{ display: 'flex', gap: 12, marginBottom: 8, fontSize: 13 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}><input type="radio" name="decision" value="approve" checked={verdictDecision === 'approve'} onChange={() => setVerdictDecision('approve')} /> 通过</label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}><input type="radio" name="decision" value="reject" checked={verdictDecision === 'reject'} onChange={() => setVerdictDecision('reject')} /> 打回</label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}><input type="radio" name="decision" value="false_positive" checked={verdictDecision === 'false_positive'} onChange={() => setVerdictDecision('false_positive')} /> 误报</label>
          </div>
          <input
            type="text"
            placeholder="复核意见..."
            value={verdictNote}
            onChange={(e) => setVerdictNote(e.target.value)}
            style={{ width: '100%', padding: '6px 8px', marginBottom: 8, boxSizing: 'border-box', border: '1px solid var(--border)', borderRadius: 4, background: '#fff' }}
          />
          <button type="button" className="primary" onClick={handleVerdictSubmit} disabled={verdictSubmitting}>
            {verdictSubmitting ? '提交中...' : '提交复核结论'}
          </button>
        </div>

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
                <li key={idx} className="risk-item" onClick={() => onSeek?.(ev.start_ms)} style={{ cursor: 'pointer' }}>
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
        
        <div className="export-actions" style={{ display: 'flex', gap: 8, marginTop: 16 }}>
          <button type="button" onClick={handleExportJson}>导出 JSON</button>
          <button type="button" onClick={handlePrint}>打印/PDF</button>
        </div>
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
              <li key={ev.id} className="risk-item" onClick={() => onSeek?.(ev.start_ms)} style={{ cursor: 'pointer' }}>
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
