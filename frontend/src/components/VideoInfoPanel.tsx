import type { VideoMetadata } from '../types'

interface Props {
  filename: string
  metadata: VideoMetadata | null
  frameCount: number | null
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

export default function VideoInfoPanel({ filename, metadata, frameCount }: Props) {
  const items: Array<{ label: string; value: string }> = [{ label: '文件名', value: filename }]

  if (metadata) {
    if (metadata.duration !== null) items.push({ label: '时长', value: formatDuration(metadata.duration) })
    if (metadata.width !== null && metadata.height !== null) {
      items.push({ label: '分辨率', value: `${metadata.width} × ${metadata.height}` })
    }
    if (metadata.fps !== null) items.push({ label: '帧率', value: `${metadata.fps} FPS` })
    if (metadata.codec !== null) items.push({ label: '编码格式', value: metadata.codec })
  }
  if (frameCount !== null) items.push({ label: '抽取帧数', value: String(frameCount) })

  return (
    <div className="video-info">
      <h3>视频信息</h3>
      <dl>
        {items.map((item) => (
          <div key={item.label} className="info-item">
            <dt>{item.label}</dt>
            <dd>{item.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
