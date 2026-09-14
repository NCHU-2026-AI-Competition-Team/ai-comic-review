import { useCallback, useEffect, useRef, useState } from 'react'
import UploadPanel from './components/UploadPanel'
import VideoInfoPanel from './components/VideoInfoPanel'
import FramesGrid from './components/FramesGrid'
import OcrPanel from './components/OcrPanel'
import AsrPanel from './components/AsrPanel'
import TimelineSwimlanes from './components/TimelineSwimlanes'
import RiskReportPanel from './components/RiskReportPanel'
import { ApiError, getFrames, getVideo, uploadVideo } from './api/videos'
import type { FramesInfo, SamplingMode, TimelineEvent, VideoJob, VideoUploadResponse } from './types'

type Phase = 'idle' | 'uploading' | 'processing' | 'processed' | 'failed' | 'error'

const POLL_INTERVAL_MS = 2000

function toJob(upload: VideoUploadResponse): VideoJob {
  return {
    video_id: upload.video_id,
    filename: upload.filename,
    status: upload.status,
    sampling: upload.sampling,
    error: null,
    metadata: upload.metadata,
    frames: upload.frames,
    created_at: new Date().toISOString(),
  }
}

export default function App() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [job, setJob] = useState<VideoJob | null>(null)
  const [framesInfo, setFramesInfo] = useState<FramesInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const cancelRef = useRef<(() => void) | null>(null)

  const [events, setEvents] = useState<{
    ocr: TimelineEvent[]
    asr: TimelineEvent[]
    vision: TimelineEvent[]
    vlm: TimelineEvent[]
  }>({
    ocr: [],
    asr: [],
    vision: [],
    vlm: [],
  })
  const [currentTimeMs, setCurrentTimeMs] = useState(0)
  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    return () => cancelRef.current?.()
  }, [])

  const loadFrames = useCallback(async (videoId: string, fallback: FramesInfo | null) => {
    try {
      setFramesInfo(await getFrames(videoId))
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setFramesInfo(fallback)
        return
      }
      setError(err instanceof Error ? err.message : '帧清单加载失败')
      setPhase('error')
    }
  }, [])

  const startPolling = useCallback(
    (videoId: string) => {
      let cancelled = false
      cancelRef.current = () => {
        cancelled = true
      }
      const tick = async () => {
        while (!cancelled) {
          try {
            const current = await getVideo(videoId)
            if (cancelled) return
            setJob(current)
            if (current.status === 'processed') {
              setPhase('processed')
              await loadFrames(videoId, current.frames)
              return
            }
            if (current.status === 'failed') {
              setError(current.error ?? '视频处理失败')
              setPhase('failed')
              return
            }
          } catch (err) {
            if (cancelled) return
            setError(err instanceof Error ? err.message : '查询处理状态失败')
            setPhase('error')
            return
          }
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
        }
      }
      void tick()
    },
    [loadFrames],
  )

  const handleSubmit = useCallback(
    async (file: File, sampling: SamplingMode) => {
      setPhase('uploading')
      setError(null)
      setFramesInfo(null)
      setEvents({ ocr: [], asr: [], vision: [], vlm: [] })
      setCurrentTimeMs(0)
      try {
        const upload = await uploadVideo(file, sampling)
        const initial = toJob(upload)
        setJob(initial)
        if (initial.status === 'processed') {
          setPhase('processed')
          await loadFrames(initial.video_id, initial.frames)
        } else if (initial.status === 'failed') {
          setPhase('failed')
        } else {
          setPhase('processing')
          startPolling(initial.video_id)
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : '上传失败')
        setPhase('error')
      }
    },
    [loadFrames, startPolling],
  )

  const handleReset = useCallback(() => {
    cancelRef.current?.()
    cancelRef.current = null
    setJob(null)
    setFramesInfo(null)
    setError(null)
    setEvents({ ocr: [], asr: [], vision: [], vlm: [] })
    setCurrentTimeMs(0)
    setPhase('idle')
  }, [])

  return (
    <div className="app">
      <header className="app-header">
        <h1>AI Comic Review</h1>
        <p>上传视频，自动解析元数据并抽取关键帧</p>
      </header>

      <main>
        {phase === 'error' && (
          <div className="error-banner">
            <span>{error}</span>
            <button type="button" onClick={handleReset}>
              返回上传
            </button>
          </div>
        )}

        {(phase === 'idle' || phase === 'uploading' || phase === 'error') && (
          <UploadPanel submitting={phase === 'uploading'} onSubmit={handleSubmit} />
        )}

        {phase === 'processing' && job && (
          <section className="panel">
            <h2>处理中</h2>
            <div className="spinner" />
            <p>正在处理《{job.filename}》，请稍候……</p>
          </section>
        )}

        {phase === 'failed' && job && (
          <section className="panel">
            <h2>处理失败</h2>
            <p className="form-error">{error ?? '视频处理失败，请稍后重试。'}</p>
            <button className="primary" type="button" onClick={handleReset}>
              重新上传
            </button>
          </section>
        )}

        {phase === 'processed' && job && (
          <div className="workbench-layout">
            <div className="workbench-top panel">
              <VideoInfoPanel
                filename={job.filename}
                metadata={job.metadata}
                sampling={framesInfo?.sampling ?? job.frames?.sampling ?? null}
                frameCount={framesInfo?.count ?? job.frames?.count ?? null}
              />
            </div>

            <div className="workbench-main">
              <div className="workbench-left">
                <video
                  ref={videoRef}
                  className="workbench-video"
                  controls
                  src={`/api/videos/${job.video_id}/file`}
                  onTimeUpdate={(e) => setCurrentTimeMs(e.currentTarget.currentTime * 1000)}
                />
              </div>
              <div className="workbench-right">
                <RiskReportPanel vlmEvents={events.vlm} />
                <div className="modality-actions">
                  <OcrPanel videoId={job.video_id} onEvents={(evs) => setEvents((prev) => ({ ...prev, ocr: evs }))} />
                  <AsrPanel videoId={job.video_id} onEvents={(evs) => setEvents((prev) => ({ ...prev, asr: evs }))} />
                </div>
              </div>
            </div>

            <TimelineSwimlanes
              duration={job.metadata?.duration ?? null}
              currentTimeMs={currentTimeMs}
              events={events}
              onSeek={(ms) => {
                if (videoRef.current) {
                  videoRef.current.currentTime = ms / 1000
                  videoRef.current.play().catch(() => {})
                }
              }}
            />

            <details className="frames-collapsible panel">
              <summary>查看抽取帧 ({framesInfo?.count ?? 0})</summary>
              {framesInfo && <FramesGrid videoId={job.video_id} framesInfo={framesInfo} />}
            </details>

            <button className="primary" type="button" onClick={handleReset} style={{ marginTop: 24 }}>
              处理新视频
            </button>
          </div>
        )}
      </main>
    </div>
  )
}
