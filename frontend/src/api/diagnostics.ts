export interface DiagnosticTrace {
  id: string
  run_id: string
  owner: string | null
  thread_id: string | null
  request_id: string | null
  profile: string | null
  label?: string | null
  state: string
  created: number
  updated: number
}
export interface DiagnosticNode {
  id: string
  parent_id: string | null
  name: string
  kind: string
  state: string
  started: number | string | null
  ended: number | string | null
  input: Record<string, unknown>
  output: Record<string, unknown>
  source: 'platform' | 'langfuse'
  model?: string | null
  usage?: Record<string, number>
  cost?: Record<string, number>
  details?: Record<string, string | number | boolean>
}
export interface DiagnosticDetail {
  trace: DiagnosticTrace
  nodes: DiagnosticNode[]
  langfuse_status: 'available' | 'pending' | 'unavailable' | 'truncated'
  complete: boolean
  related_tasks: string[]
}
export async function readDiagnostics<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch('/api/v1/hermes/diagnostics' + path, {
    credentials: 'same-origin',
    cache: 'no-store',
    signal,
  })
  if (!response.ok) {
    if (response.status === 404) throw new Error('记录不存在或当前账号无权查看。')
    if (response.status === 503) throw new Error('调用链服务尚未启用或暂时不可用。')
    throw new Error('读取失败，请重试。')
  }
  return (await response.json()) as T
}
export const stateLabel = (value: string) =>
  ({
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '已中断',
    succeeded: '已成功',
    partial: '部分完成',
    unknown: '结束状态缺失',
    skipped: '未执行',
    recorded: '已记录',
    queued: '排队中',
    pending: '待确认',
    awaiting_confirmation: '待确认',
    cancel_requested: '取消中',
    paused: '已暂停',
    expired: '已过期',
    finished: '执行已结束',
  })[value] ?? value
export const timestamp = (value: number | string | null) =>
  value == null ? Number.NaN : typeof value === 'number' ? value * 1000 : Date.parse(value)
export const duration = (node: DiagnosticNode) =>
  node.ended == null || node.started == null
    ? null
    : Math.max(0, timestamp(node.ended) - timestamp(node.started))
