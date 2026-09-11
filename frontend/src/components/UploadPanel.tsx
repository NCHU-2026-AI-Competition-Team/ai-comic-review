import { useCallback, useRef, useState } from 'react'
import type { UploadPayload } from '../types'

interface Props {
  submitting: boolean
  onSubmit: (payload: UploadPayload) => void
}

const ACCEPTED = ['image/png', 'image/jpeg', 'image/webp']
const MAX_FILES = 60

interface PageItem {
  id: string
  file: File
  previewUrl: string
}

export default function UploadPanel({ submitting, onSubmit }: Props) {
  const [title, setTitle] = useState('')
  const [genre, setGenre] = useState('少年漫画')
  const [authorNote, setAuthorNote] = useState('')
  const [pages, setPages] = useState<PageItem[]>([])
  const [dragOver, setDragOver] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const addFiles = useCallback((files: FileList | File[]) => {
    setError(null)
    const incoming = Array.from(files)
    const rejected = incoming.filter((f) => !ACCEPTED.includes(f.type))
    if (rejected.length > 0) {
      setError(`已忽略 ${rejected.length} 个不支持的文件，仅支持 PNG / JPEG / WebP`)
    }
    const accepted = incoming.filter((f) => ACCEPTED.includes(f.type))
    setPages((prev) => {
      const room = MAX_FILES - prev.length
      if (accepted.length > room) {
        setError(`最多上传 ${MAX_FILES} 页，超出部分已忽略`)
      }
      const items = accepted.slice(0, Math.max(room, 0)).map((file) => ({
        id: `${file.name}-${file.size}-${crypto.randomUUID()}`,
        file,
        previewUrl: URL.createObjectURL(file),
      }))
      return [...prev, ...items]
    })
  }, [])

  const removePage = (id: string) => {
    setPages((prev) => {
      const target = prev.find((p) => p.id === id)
      if (target) URL.revokeObjectURL(target.previewUrl)
      return prev.filter((p) => p.id !== id)
    })
  }

  const movePage = (id: string, delta: number) => {
    setPages((prev) => {
      const index = prev.findIndex((p) => p.id === id)
      const next = index + delta
      if (index < 0 || next < 0 || next >= prev.length) return prev
      const copy = [...prev]
      const [item] = copy.splice(index, 1)
      copy.splice(next, 0, item)
      return copy
    })
  }

  const handleSubmit = () => {
    if (pages.length === 0) {
      setError('请先上传至少一页漫画')
      return
    }
    onSubmit({
      title: title.trim(),
      genre,
      authorNote: authorNote.trim(),
      files: pages.map((p) => p.file),
    })
  }

  return (
    <section className="panel upload-panel">
      <h2>上传作品</h2>

      <div className="field">
        <label htmlFor="title">作品标题</label>
        <input
          id="title"
          type="text"
          placeholder="例如：星之轨迹 第一话"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          maxLength={80}
        />
      </div>

      <div className="field">
        <label htmlFor="genre">类型</label>
        <select id="genre" value={genre} onChange={(e) => setGenre(e.target.value)}>
          <option>少年漫画</option>
          <option>少女漫画</option>
          <option>青年漫画</option>
          <option>搞笑漫画</option>
          <option>悬疑漫画</option>
          <option>其他</option>
        </select>
      </div>

      <div className="field">
        <label htmlFor="note">作者备注（可选）</label>
        <textarea
          id="note"
          rows={3}
          placeholder="希望 AI 重点关注的方面，例如叙事节奏、分镜……"
          value={authorNote}
          onChange={(e) => setAuthorNote(e.target.value)}
          maxLength={500}
        />
      </div>

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
          addFiles(e.dataTransfer.files)
        }}
      >
        <p>拖拽页面图片到此处，或点击选择文件</p>
        <p className="hint">支持 PNG / JPEG / WebP，按上传顺序作为页码，最多 {MAX_FILES} 页</p>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED.join(',')}
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files) addFiles(e.target.files)
            e.target.value = ''
          }}
        />
      </div>

      {error && <p className="form-error">{error}</p>}

      {pages.length > 0 && (
        <>
          <p className="page-count">已选择 {pages.length} 页</p>
          <ul className="page-grid">
            {pages.map((page, index) => (
              <li key={page.id} className="page-item">
                <img src={page.previewUrl} alt={`第 ${index + 1} 页`} />
                <span className="page-index">{index + 1}</span>
                <div className="page-actions">
                  <button type="button" title="前移" onClick={() => movePage(page.id, -1)}>
                    ←
                  </button>
                  <button type="button" title="后移" onClick={() => movePage(page.id, 1)}>
                    →
                  </button>
                  <button type="button" title="移除" onClick={() => removePage(page.id)}>
                    ×
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <button
        className="primary"
        type="button"
        disabled={submitting || pages.length === 0}
        onClick={handleSubmit}
      >
        {submitting ? '提交中…' : '开始 AI 评审'}
      </button>
    </section>
  )
}
