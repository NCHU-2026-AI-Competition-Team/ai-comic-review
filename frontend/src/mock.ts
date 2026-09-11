import type { ReviewResult } from './types'

export function buildMockResult(title: string): ReviewResult {
  return {
    review_id: 'demo-0001',
    title: title || '未命名作品',
    status: 'completed',
    overall_score: 82,
    summary:
      '作品整体完成度较高，画面表现力出色，角色情绪传达准确。主要提升空间在于中段叙事节奏与部分分镜的信息密度，建议精简对白并加强关键情节的视觉铺垫。',
    dimensions: [
      { name: '画面表现', score: 88, max_score: 100, comment: '线条干净，黑白灰层次丰富，部分跨页构图很有冲击力。' },
      { name: '叙事节奏', score: 74, max_score: 100, comment: '开场抓人，但第 6-9 页节奏拖沓，信息重复。' },
      { name: '角色塑造', score: 85, max_score: 100, comment: '主角动机清晰，配角略显工具化。' },
      { name: '对白文案', score: 78, max_score: 100, comment: '对白自然，但部分说明性台词可以交给画面表达。' },
      { name: '分镜设计', score: 83, max_score: 100, comment: '视线引导流畅，少数格子信息过载。' },
    ],
    issues: [
      {
        page: 3,
        severity: 'warning',
        category: '分镜',
        description: '第 3 页第 2 格与第 3 格动作衔接跳跃，读者难以理解角色如何移动到门口。',
        suggestion: '在两格之间插入一个过渡格，或调整第 2 格构图让动作方向更明确。',
      },
      {
        page: 7,
        severity: 'error',
        category: '叙事',
        description: '第 7 页揭示关键伏笔，但前文没有任何铺垫，显得突兀。',
        suggestion: '在第 4-5 页增加一处视觉暗示（如背景道具或角色一句双关台词）。',
      },
      {
        page: 10,
        severity: 'info',
        category: '对白',
        description: '结尾独白略长，削弱了最后一格的画面情绪。',
        suggestion: '将独白压缩到两句以内，把情绪留给画面收束。',
      },
    ],
    created_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
  }
}
