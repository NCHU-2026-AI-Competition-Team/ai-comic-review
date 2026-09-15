import { useEffect, useState } from 'react';
import { getVideos } from '../api/videos';
import type { VideoSummary } from '../types';

interface HistoryListProps {
  onSelect: (videoId: string) => void;
  activeVideoId?: string | null;
}

export default function HistoryList({ onSelect, activeVideoId }: HistoryListProps) {
  const [videos, setVideos] = useState<VideoSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getVideos()
      .then(data => {
        setVideos(data || []);
      })
      .catch(() => {
        // Silent fallback
        setVideos([]);
      })
      .finally(() => {
        setLoading(false);
      });
  }, []);

  if (loading) {
    return <div className="history-list-loading">加载历史任务中...</div>;
  }

  if (videos.length === 0) {
    return null; // Silent degrade or just empty
  }

  return (
    <div className="history-panel">
      <div className="history-header">历史任务</div>
      <div className="history-list">
        {videos.map(v => (
          <div
            key={v.video_id}
            className={`history-item ${activeVideoId === v.video_id ? 'active' : ''}`}
            onClick={() => onSelect(v.video_id)}
          >
            <div className="history-item-title">{v.filename}</div>
            <div className="history-item-time">{new Date(v.created_at).toLocaleString('zh-CN')}</div>
            <div className="history-item-badges">
              <Badge name="OCR" mod={v.modalities?.ocr} />
              <Badge name="ASR" mod={v.modalities?.asr} />
              <Badge name="VLM" mod={v.modalities?.vlm} />
              {v.verdict ? (
                <span className={`badge verdict ${v.verdict.decision}`}>
                  {v.verdict.decision === 'approve' ? '通过' : v.verdict.decision === 'reject' ? '驳回' : '误报'}
                </span>
              ) : (
                <span className="badge verdict pending">待复核</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Badge({ name, mod }: { name: string; mod?: { ran: boolean; event_count: number } }) {
  if (!mod || !mod.ran) {
    return <span className="badge gray" title={`${name} 未运行`}>{name}</span>;
  }
  const hasEvents = mod.event_count > 0;
  return (
    <span className={`badge ${hasEvents ? 'red' : 'green'}`} title={`${name} ${hasEvents ? '检出风险' : '通过'}`}>
      {name} {mod.event_count > 0 ? mod.event_count : ''}
    </span>
  );
}
