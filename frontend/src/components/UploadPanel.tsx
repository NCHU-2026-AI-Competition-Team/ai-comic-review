import { useCallback, useRef, useState } from 'react'
import type { SamplingMode } from '../types'

interface SamplingOptions {
  sampling: SamplingMode
  frame_fps?: number
  scene_threshold?: number
  scene_max_frames?: number
}

interface Props {
  submitting: boolean
  onSubmit: (file: File, options: SamplingOptions) => void
}

const ACCEPTED_EXTENSIONS = ['.mp4', '.mov', '.mkv']

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(1)} KB`
}

function isAccepted(file: File): boolean {
  const name = file.name.toLowerCase()
  return ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext))
}

export default function UploadPanel({ submitting, onSubmit }: Props) {
  const [file, setFile] = useState<File | null>(null)
  const [sampling, setSampling] = useState<SamplingMode>('fixed_fps')
  const [frameFps, setFrameFps] = useState('')
  const [sceneThreshold, setSceneThreshold] = useState('')
  const [sceneMaxFrames, setSceneMaxFrames] = useState('')
  const [dragOver, setDragOver] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const pick = useCallback((candidates: FileList | File[]) => {
    setError(null)
    const first = Array.from(candidates)[0]
    if (!first) return
    if (!isAccepted(first)) {
      setError(`不支持的格式，仅允许 ${ACCEPTED_EXTENSIONS.join(' / ')} 视频文件`)
      return
    }
    setFile(first)
  }, [])

  const handleSubmit = useCallback(() => {
    if (!file) return
    setError(null)

    const options: SamplingOptions = { sampling }

    if (sampling === 'fixed_fps') {
      if (frameFps.trim() !== '') {
        const fps = Number(frameFps)
        if (Number.isNaN(fps) || fps <= 0) {
          setError('帧率必须是大于 0 的有效数字')
          return
        }
        options.frame_fps = fps
      }
    } else if (sampling === 'scene') {
      if (sceneThreshold.trim() !== '') {
        const threshold = Number(sceneThreshold)
        if (Number.isNaN(threshold) || threshold < 0 || threshold > 1) {
          setError('场景阈值必须是 0-1 之间的有效数字')
          return
        }
        options.scene_threshold = threshold
      }
      if (sceneMaxFrames.trim() !== '') {
        const maxFrames = Number(sceneMaxFrames)
        if (Number.isNaN(maxFrames) || maxFrames <= 0 || !Number.isInteger(maxFrames)) {
          setError('最大帧数必须是大于 0 的整数')
          return
        }
        options.scene_max_frames = maxFrames
      }
    }

    onSubmit(file, options)
  }, [file, sampling, frameFps, sceneThreshold, sceneMaxFrames, onSubmit])

  return (
    <section className="panel upload-panel">
      <h2>上传视频</h2>
      <p className="hint">上传视频后，系统将解析元数据并抽取关键帧</p>

      <div
        className={`dropzone${dragOver ? ' drag-over' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          pick(e.dataTransfer.files)
        }}
      >
        <p>拖拽视频文件到此处，或点击选择文件</p>
        <p className="hint">支持 {ACCEPTED_EXTENSIONS.join(' / ')}，单个文件</p>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED_EXTENSIONS.join(',')}
          hidden
          onChange={(e) => {
            if (e.target.files) pick(e.target.files)
            e.target.value = ''
          }}
        />
      </div>

      {error && <p className="form-error">{error}</p>}

      <label className="sampling-select">
        采样模式
        <select value={sampling} onChange={(e) => {
          setSampling(e.target.value as SamplingMode)
          setError(null)
        }}>
          <option value="fixed_fps">固定帧率</option>
          <option value="scene">镜头切换检测</option>
        </select>
      </label>

      <details style={{ marginBottom: 16 }}>
        <summary style={{ cursor: 'pointer', fontSize: 14, color: 'var(--text-dim)', marginBottom: 8 }}>高级设置</summary>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: '12px', background: 'var(--bg)', borderRadius: 8, border: '1px solid var(--border)' }}>
          {sampling === 'fixed_fps' ? (
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14 }}>
              帧率 (fps):
              <input 
                type="number" 
                step="0.1" 
                placeholder="默认 2.0" 
                value={frameFps} 
                onChange={(e) => setFrameFps(e.target.value)} 
                style={{ background: 'var(--panel)', color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 4, padding: '4px 8px' }} 
              />
            </label>
          ) : (
            <>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14 }}>
                场景阈值 (0-1):
                <input 
                  type="number" 
                  step="0.01" 
                  min="0" 
                  max="1" 
                  placeholder="默认 0.4" 
                  value={sceneThreshold} 
                  onChange={(e) => setSceneThreshold(e.target.value)} 
                  style={{ background: 'var(--panel)', color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 4, padding: '4px 8px' }} 
                />
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14 }}>
                最大帧数:
                <input 
                  type="number" 
                  step="1" 
                  min="1" 
                  placeholder="默认 500" 
                  value={sceneMaxFrames} 
                  onChange={(e) => setSceneMaxFrames(e.target.value)} 
                  style={{ background: 'var(--panel)', color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 4, padding: '4px 8px' }} 
                />
              </label>
            </>
          )}
        </div>
      </details>

      {file && (
        <div className="file-card">
          <span className="file-name">{file.name}</span>
          <span className="file-size">{formatSize(file.size)}</span>
          <button type="button" className="file-remove" onClick={() => setFile(null)}>
            移除
          </button>
        </div>
      )}

      <button className="primary" type="button" disabled={submitting || !file} onClick={handleSubmit}>
        {submitting ? '上传中…' : '开始处理'}
      </button>
    </section>
  )
}
