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
  failure?: DiagnosticFailure | null
}
export interface DiagnosticFailure {
  code: string
  title: string
  message?: string
  suggestion: string
  retryable: boolean
  error_id?: string
  exception_type?: string
  http_status?: number
  validation?: { field: (string | number)[]; code: string; message: string }[]
  frames?: { file: string; line: number; function: string }[]
}
export function tokenUsage(node: DiagnosticNode) {
  const raw = node.usage ?? {}
  const number = (key: string) =>
    typeof raw[key] === 'number' && Number.isFinite(raw[key]) && raw[key] >= 0 ? raw[key]! : null
  const cached = number('cache_read_input_tokens') ?? 0
  const written = number('cache_creation_input_tokens') ?? 0
  // Official plugin: input is uncached; reasoning is already part of output.
  const input =
    number('prompt_tokens') ??
    (number('input') != null || cached || written
      ? (number('input') ?? 0) + cached + written
      : null)
  const output = number('output') ?? number('completion_tokens')
  return {
    input,
    output,
    cached,
    written,
    reasoning: number('reasoning_tokens'),
    total: input != null && output != null ? input + output : null,
  }
}
export function summarizeTokenUsage(nodes: DiagnosticNode[]) {
  const generations = nodes.filter((node) => node.kind === 'generation')
  const known = generations.map(tokenUsage).filter((usage) => usage.total != null)
  return {
    generations: generations.length,
    known: known.length,
    state: !generations.length
      ? 'no_generation'
      : !known.length
        ? 'missing'
        : known.length < generations.length
          ? 'partial'
          : 'complete',
    total: known.length ? known.reduce((sum, row) => sum + row.total!, 0) : null,
    input: known.reduce((sum, row) => sum + row.input!, 0),
    output: known.reduce((sum, row) => sum + row.output!, 0),
    cached: known.reduce((sum, row) => sum + row.cached, 0),
    written: known.reduce((sum, row) => sum + row.written, 0),
  }
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
