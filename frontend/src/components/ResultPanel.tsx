import type { ReviewResult, IssueSeverity } from '../types'

interface Props {
  result: ReviewResult
  onReset: () => void
}

const SEVERITY_LABEL: Record<IssueSeverity, string> = {
  info: '提示',
  warning: '警告',
  error: '严重',
}

function scoreClass(score: number): string {
  if (score >= 85) return 'score good'
  if (score >= 70) return 'score mid'
  return 'score low'
}

export default function ResultPanel({ result, onReset }: Props) {
  if (result.status === 'pending' || result.status === 'processing') {
    return (
      <section className="panel result-panel">
        <h2>评审进行中</h2>
        <div className="spinner" />
        <p>AI 正在评审《{result.title || '未命名作品'}》，请稍候……</p>
        <p className="hint">当前状态：{result.status === 'pending' ? '排队中' : '分析中'}</p>
      </section>
    )
  }

  if (result.status === 'failed') {
    return (
      <section className="panel result-panel">
        <h2>评审失败</h2>
        <p className="form-error">评审任务执行失败，请稍后重试。</p>
        <button className="primary" type="button" onClick={onReset}>
          重新上传
        </button>
      </section>
    )
  }

  return (
    <section className="panel result-panel">
      <header className="result-header">
        <div>
          <h2>评审结果</h2>
          <p className="result-title">《{result.title || '未命名作品'}》</p>
        </div>
        {result.overall_score !== null && (
          <div className={scoreClass(result.overall_score)}>
            <span className="score-number">{result.overall_score}</span>
            <span className="score-label">综合评分</span>
          </div>
        )}
      </header>

      {result.summary && (
        <div className="summary">
          <h3>总体评价</h3>
          <p>{result.summary}</p>
        </div>
      )}

      {result.dimensions.length > 0 && (
        <div className="dimensions">
          <h3>维度评分</h3>
          {result.dimensions.map((dim) => {
            const pct = Math.round((dim.score / dim.max_score) * 100)
            return (
              <div key={dim.name} className="dimension">
                <div className="dimension-head">
                  <span>{dim.name}</span>
                  <span>
                    {dim.score} / {dim.max_score}
                  </span>
                </div>
                <div className="bar">
                  <div className="bar-fill" style={{ width: `${pct}%` }} />
                </div>
                <p className="dimension-comment">{dim.comment}</p>
              </div>
            )
          })}
        </div>
      )}

      {result.issues.length > 0 && (
        <div className="issues">
          <h3>问题与建议（{result.issues.length}）</h3>
          <ul>
            {result.issues.map((issue, i) => (
              <li key={i} className={`issue severity-${issue.severity}`}>
                <div className="issue-head">
                  <span className={`badge severity-${issue.severity}`}>
                    {SEVERITY_LABEL[issue.severity]}
                  </span>
                  <span className="issue-location">
                    第 {issue.page} 页 · {issue.category}
                  </span>
                </div>
                <p>{issue.description}</p>
                <p className="issue-suggestion">建议：{issue.suggestion}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      <button className="primary" type="button" onClick={onReset}>
        评审新作品
      </button>
    </section>
  )
}
