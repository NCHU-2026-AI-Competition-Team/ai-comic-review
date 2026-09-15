import { useState } from 'react'
import type { SamplingInfo, VideoMetadata, VideoJob } from '../types'

interface Props {
  filename: string
  metadata: VideoMetadata | null
  sampling: SamplingInfo | null
  frameCount: number | null
  job?: VideoJob | null
}

function formatSampling(sampling: SamplingInfo, job?: VideoJob | null): string {
  if (sampling.method === 'scene_change') {
    const maxFrames = job?.scene_max_frames ? `，最大帧数 ${job.scene_max_frames}` : ''
    return `镜头切换检测（生效阈值 ${sampling.threshold}${maxFrames}）`
  }
  return `固定帧率（生效帧率 ${sampling.fps} FPS）`
}

function formatDuration(seconds: number): string {
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const mm = String(m).padStart(2, '0')
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

export default function VideoInfoPanel({ filename, metadata, sampling, frameCount, job }: Props) {
  const [expanded, setExpanded] = useState(false)
  const items: Array<{ label: string; value: string }> = [{ label: '文件名', value: filename }]

  if (metadata) {
    if (metadata.duration !== null) items.push({ label: '时长', value: formatDuration(metadata.duration) })
    if (metadata.width !== null && metadata.height !== null) {
      items.push({ label: '分辨率', value: `${metadata.width} × ${metadata.height}` })
    }
    if (metadata.fps !== null) items.push({ label: '帧率', value: `${metadata.fps} FPS` })
    if (metadata.codec !== null) items.push({ label: '编码格式', value: metadata.codec })
  }
  if (sampling) items.push({ label: '采样方式', value: formatSampling(sampling, job) })
  if (frameCount !== null) items.push({ label: '抽取帧数', value: String(frameCount) })

  const summaryParts = [
    filename,
    metadata?.duration != null ? formatDuration(metadata.duration) : '',
    metadata?.width != null && metadata?.height != null ? `${metadata.width}×${metadata.height}` : '',
    metadata?.fps != null ? `${metadata.fps}fps` : ''
  ].filter(Boolean)

  return (
    <div className="video-info" style={{ cursor: 'pointer', userSelect: 'none' }} onClick={() => setExpanded(!expanded)}>
      {!expanded ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '14px', color: 'var(--text-main)' }}>
          <span style={{ fontWeight: 600 }}>{summaryParts.join(' · ')}</span>
          {sampling && (
             <span className="badge gray" style={{ marginLeft: 8 }}>
               {sampling.method === 'scene_change' ? '镜头切换' : `固定帧率 ${sampling.fps}FPS`}
             </span>
          )}
          {frameCount !== null && (
             <span className="badge gray">
               {frameCount} 帧
             </span>
          )}
          <span style={{ marginLeft: 'auto', color: 'var(--text-dim)', fontSize: '12px' }}>点击展开 ▾</span>
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ margin: 0 }}>视频信息</h3>
            <span style={{ color: 'var(--text-dim)', fontSize: '12px' }}>点击收起 ▴</span>
          </div>
          <dl>
            {items.map((item) => (
              <div key={item.label} className="info-item" style={{ cursor: 'text' }} onClick={(e) => e.stopPropagation()}>
                <dt>{item.label}</dt>
                <dd>{item.value}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </div>
  )
}
