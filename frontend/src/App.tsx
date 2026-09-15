import { useCallback, useEffect, useRef, useState } from 'react'
import UploadPanel from './components/UploadPanel'
import VideoInfoPanel from './components/VideoInfoPanel'
import FramesGrid from './components/FramesGrid'
import OcrPanel from './components/OcrPanel'
import AsrPanel from './components/AsrPanel'
import TimelineSwimlanes from './components/TimelineSwimlanes'
import RiskReportPanel from './components/RiskReportPanel'
import HistoryList from './components/HistoryList'
import { ApiError, getEvents, getFrames, getVideo, uploadVideo, runOcr, runAsr, runReview } from './api/videos'
import type { FramesInfo, SamplingMode, TimelineEvent, VideoJob, VideoUploadResponse } from './types'

type Phase = 'idle' | 'uploading' | 'processing' | 'processed' | 'failed' | 'error'
type WorkflowState = 'idle' | 'running' | 'success' | 'failed'
interface WorkflowStatus {
  state: WorkflowState
  stage: 'ocr' | 'asr' | 'review' | null
  message: string
  retry: boolean
}

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
  // 当前生效的视频 id：异步轮询/请求返回时校验，防止过期结果覆盖新视频状态
  const activeVideoIdRef = useRef<string | null>(null)

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
  // 视频真实时长（秒）：onLoadedMetadata 后填入，后端 metadata.duration 仅作初值
  const [videoDurationSec, setVideoDurationSec] = useState<number | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)

  const [workflowStatus, setWorkflowStatus] = useState<WorkflowStatus>({ state: 'idle', stage: null, message: '', retry: false })
  const [reportRefreshKey, setReportRefreshKey] = useState(0)

  const handleRunWorkflow = useCallback(async (videoId: string) => {
    setWorkflowStatus({ state: 'running', stage: 'ocr', message: '正在执行 OCR...', retry: false })

    const callWithRetry = async <T,>(apiCall: () => Promise<T>): Promise<T> => {
      try {
        return await apiCall()
      } catch (err: any) {
        if (err instanceof ApiError && err.status === 502) {
          setWorkflowStatus((s) => ({ ...s, message: '云端冷启动，重试中...', retry: true }))
          return await apiCall()
        }
        throw err
      }
    }

    try {
      const ocrRes = await callWithRetry(() => runOcr(videoId))
      setWorkflowStatus({ state: 'running', stage: 'asr', message: `OCR 完成 (事件: ${ocrRes.event_count})，正在执行 ASR...`, retry: false })

      const asrRes = await callWithRetry(() => runAsr(videoId))
      setWorkflowStatus({ state: 'running', stage: 'review', message: `ASR 完成 (事件: ${asrRes.event_count})，正在执行多模态审核...`, retry: false })

      await callWithRetry(() => runReview(videoId))

      // Refresh events
      const [newOcr, newAsr, newVlm] = await Promise.all([
        getEvents(videoId, 'ocr'),
        getEvents(videoId, 'asr'),
        getEvents(videoId, 'vlm'),
      ])
      if (activeVideoIdRef.current !== videoId) return

      setEvents((prev) => ({ ...prev, ocr: newOcr, asr: newAsr, vlm: newVlm }))
      setReportRefreshKey((k) => k + 1)
      setWorkflowStatus({ state: 'success', stage: null, message: '全流程审核完成', retry: false })
    } catch (err: any) {
      if (activeVideoIdRef.current !== videoId) return
      setWorkflowStatus((s) => ({
        state: 'failed',
        stage: s.stage,
        message: `阶段 ${s.stage?.toUpperCase()} 失败: ${err.message || '未知错误'}`,
        retry: false,
      }))
    }
  }, [])

  useEffect(() => {
    return () => cancelRef.current?.()
  }, [])

  const loadFrames = useCallback(async (videoId: string, fallback: FramesInfo | null) => {
    try {
      const frames = await getFrames(videoId)
      if (activeVideoIdRef.current !== videoId) return
      setFramesInfo(frames)
    } catch (err) {
      if (activeVideoIdRef.current !== videoId) return
      if (err instanceof ApiError && err.status === 404) {
        setFramesInfo(fallback)
        return
      }
      setError(err instanceof Error ? err.message : '帧清单加载失败')
      setPhase('error')
    }
  }, [])

  const loadVlmEvents = useCallback(async (videoId: string) => {
    try {
      const vlmEvents = await getEvents(videoId, 'vlm')
      if (activeVideoIdRef.current !== videoId) return
      setEvents((prev) => ({ ...prev, vlm: vlmEvents }))
    } catch {
      // VLM 结果未生成（404）或读取失败时保持空态，不影响其他面板
    }
  }, [])

  const startPolling = useCallback(
    (videoId: string) => {
      cancelRef.current?.() // 先取消上一轮轮询，避免旧任务继续写状态
      let cancelled = false
      cancelRef.current = () => {
        cancelled = true
      }
      const tick = async () => {
        while (!cancelled) {
          try {
            const current = await getVideo(videoId)
            if (cancelled || activeVideoIdRef.current !== videoId) return
            setJob(current)
            if (current.status === 'processed') {
              setPhase('processed')
              await loadFrames(videoId, current.frames)
              await loadVlmEvents(videoId)
              return
            }
            if (current.status === 'failed') {
              setError(current.error ?? '视频处理失败')
              setPhase('failed')
              return
            }
          } catch (err) {
            if (cancelled || activeVideoIdRef.current !== videoId) return
            setError(err instanceof Error ? err.message : '查询处理状态失败')
            setPhase('error')
            return
          }
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
        }
      }
      void tick()
    },
    [loadFrames, loadVlmEvents],
  )

  const handleSubmit = useCallback(
    async (file: File, options: { sampling: SamplingMode; frame_fps?: number; scene_threshold?: number; scene_max_frames?: number }) => {
      cancelRef.current?.() // 取消旧视频的轮询
      activeVideoIdRef.current = null
      setPhase('uploading')
      setError(null)
      setFramesInfo(null)
      setEvents({ ocr: [], asr: [], vision: [], vlm: [] })
      setCurrentTimeMs(0)
      setVideoDurationSec(null)
      setWorkflowStatus({ state: 'idle', stage: null, message: '', retry: false })
      try {
        const upload = await uploadVideo(file, options)
        const initial = toJob(upload)
        activeVideoIdRef.current = initial.video_id
        setJob(initial)
        if (initial.status === 'processed') {
          setPhase('processed')
          await loadFrames(initial.video_id, initial.frames)
          await loadVlmEvents(initial.video_id)
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
    [loadFrames, loadVlmEvents, startPolling],
  )

  const handleReset = useCallback(() => {
    cancelRef.current?.()
    cancelRef.current = null
    activeVideoIdRef.current = null
    setJob(null)
    setFramesInfo(null)
    setError(null)
    setEvents({ ocr: [], asr: [], vision: [], vlm: [] })
    setCurrentTimeMs(0)
    setVideoDurationSec(null)
    setPhase('idle')
    setWorkflowStatus({ state: 'idle', stage: null, message: '', retry: false })
  }, [])

  const handleSelectHistory = useCallback(async (videoId: string) => {
    cancelRef.current?.()
    activeVideoIdRef.current = videoId
    setPhase('processing')
    setJob(null)
    setFramesInfo(null)
    setError(null)
    setEvents({ ocr: [], asr: [], vision: [], vlm: [] })
    setCurrentTimeMs(0)
    setVideoDurationSec(null)
    setWorkflowStatus({ state: 'idle', stage: null, message: '', retry: false })

    try {
      const current = await getVideo(videoId)
      if (activeVideoIdRef.current !== videoId) return
      setJob(current)
      if (current.status === 'processed') {
        setPhase('processed')
        await loadFrames(videoId, current.frames)
        await loadVlmEvents(videoId)
      } else if (current.status === 'failed') {
        setError(current.error ?? '视频处理失败')
        setPhase('failed')
      } else {
        setPhase('processing')
        startPolling(videoId)
      }
    } catch (err) {
      if (activeVideoIdRef.current !== videoId) return
      setError(err instanceof Error ? err.message : '加载历史任务失败')
      setPhase('error')
    }
  }, [loadFrames, loadVlmEvents, startPolling])

  const [highlightMs, setHighlightMs] = useState<number | null>(null)

  const handleSeekAndPause = useCallback((ms: number) => {
    setHighlightMs(ms)
    if (videoRef.current) {
      // clip logic: prevent seeking beyond duration, handle NaN safely
      const d = videoRef.current.duration
      if (Number.isFinite(d) && d > 0) {
        videoRef.current.currentTime = Math.min(Math.max(ms / 1000, 0), d)
      } else {
        videoRef.current.currentTime = ms / 1000
      }
      videoRef.current.pause()
    }
  }, [])

  return (
    <div className="app-container">
      <aside className="app-sidebar">
        <div className="brand">
          <h1>AI 漫审</h1>
          <p>短视频内容安全控制台</p>
        </div>
        <HistoryList onSelect={handleSelectHistory} activeVideoId={job?.video_id} />
      </aside>

      <main className="app-main">
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
                job={job}
              />
            </div>

            <div className="workbench-main">
              <div className="workbench-left">
                <video
                  ref={videoRef}
                  className="workbench-video"
                  controls
                  src={`/api/videos/${job.video_id}/file`}
                  onLoadedMetadata={(e) => {
                    const realDuration = e.currentTarget.duration
                    if (Number.isFinite(realDuration) && realDuration > 0) {
                      setVideoDurationSec(realDuration)
                    }
                  }}
                  onTimeUpdate={(e) => setCurrentTimeMs(e.currentTarget.currentTime * 1000)}
                />
              </div>
              <div className="workbench-right">
                <RiskReportPanel videoId={job.video_id} vlmEvents={events.vlm} refreshKey={reportRefreshKey} onSeek={handleSeekAndPause} />
                
                <div className="workflow-panel panel" style={{ marginBottom: 16 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <h3 style={{ margin: 0, fontSize: 15 }}>全流程自动审核</h3>
                    <button
                      className="primary"
                      type="button"
                      disabled={workflowStatus.state === 'running'}
                      onClick={() => handleRunWorkflow(job.video_id)}
                    >
                      {workflowStatus.state === 'running' ? '审核中...' : '一键审核'}
                    </button>
                  </div>
                  {workflowStatus.state !== 'idle' && (
                    <div className={`workflow-status ${workflowStatus.state}`} style={{ marginTop: 12, padding: '8px 12px', background: 'var(--bg)', borderRadius: 4, fontSize: 13, display: 'flex', alignItems: 'center' }}>
                      {workflowStatus.state === 'running' && <div className="spinner" style={{ width: 12, height: 12, marginRight: 8, borderWidth: 2, display: 'inline-block' }} />}
                      <span style={{ color: workflowStatus.state === 'failed' ? 'var(--error)' : workflowStatus.state === 'success' ? '#4caf50' : 'inherit' }}>
                        {workflowStatus.message}
                      </span>
                    </div>
                  )}
                </div>

                <div className="modality-actions">
                  <OcrPanel videoId={job.video_id} onEvents={(evs) => setEvents((prev) => ({ ...prev, ocr: evs }))} />
                  <AsrPanel videoId={job.video_id} onEvents={(evs) => setEvents((prev) => ({ ...prev, asr: evs }))} />
                </div>
              </div>
            </div>

            <TimelineSwimlanes
              duration={videoDurationSec ?? job.metadata?.duration ?? null}
              currentTimeMs={currentTimeMs}
              events={events}
              onSeek={handleSeekAndPause}
            />

            <details className="frames-collapsible panel">
              <summary>查看抽取帧 ({framesInfo?.count ?? 0})</summary>
              {framesInfo && <FramesGrid videoId={job.video_id} framesInfo={framesInfo} highlightMs={highlightMs} />}
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
