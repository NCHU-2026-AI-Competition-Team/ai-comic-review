import { useCallback, useRef, useState } from 'react'
import type { SamplingMode } from '../types'

interface Props {
  submitting: boolean
  onSubmit: (file: File, sampling: SamplingMode) => void
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
        <select value={sampling} onChange={(e) => setSampling(e.target.value as SamplingMode)}>
          <option value="fixed_fps">固定帧率</option>
          <option value="scene">镜头切换检测</option>
        </select>
      </label>

      {file && (
        <div className="file-card">
          <span className="file-name">{file.name}</span>
          <span className="file-size">{formatSize(file.size)}</span>
          <button type="button" className="file-remove" onClick={() => setFile(null)}>
            移除
          </button>
        </div>
      )}

      <button className="primary" type="button" disabled={submitting || !file} onClick={() => file && onSubmit(file, sampling)}>
        {submitting ? '上传中…' : '开始处理'}
      </button>
    </section>
  )
}
