export interface MemoryStatus {
  enabled: boolean
  epoch: number
  queue: Record<string, number>
  deletion: 'none' | 'pending' | 'completed'
}

export function memorySummary(raw: string): string {
  try {
    const data = JSON.parse(raw)
    if (data && typeof data === 'object' && ('representation' in data || 'peer_card' in data))
      return [data.representation, data.peer_card]
        .filter(
          (value) =>
            value != null &&
            value !== '' &&
            JSON.stringify(value) !== '{}' &&
            JSON.stringify(value) !== '[]',
        )
        .map((value) => (typeof value === 'string' ? value : JSON.stringify(value, null, 2)))
        .join('\n\n')
  } catch {
    /* Truncated or plain text stays visible without evaluation. */
  }
  return raw
}
export interface Correction {
  request_id: string
  content: string
  state: string
  created: number
}
export interface LearningStatus {
  enabled: boolean
  auto_trials: boolean
  verified_experiences: number
  scheduler_observed: boolean
  thresholds: Record<string, number>
  patterns: { pattern: string; successes: number; distinct_inputs: number }[]
  jobs: {
    id: string
    state: string
    created: number
    draft?: string
    trial?: string
    error?: string
  }[]
}
export async function memoryRequest<T>(path = '', method = 'GET', body?: unknown): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 30000)
  try {
    const response = await fetch('/api/v1/hermes/memory' + path, {
      method,
      body: body === undefined ? undefined : JSON.stringify(body),
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      redirect: 'error',
      signal: controller.signal,
    })
    if (!response.ok)
      throw Error(
        response.status === 503
          ? '记忆服务未配置或暂不可用'
          : response.status === 409
            ? '更正内容冲突或当前记忆已暂停'
            : '请求失败，请刷新核对',
      )
    return (await response.json()) as T
  } catch (e) {
    throw Error(
      method === 'GET' ? '读取失败，请刷新重试' : '请求结果待核实，请先刷新；勿更换请求号重复提交',
      { cause: e },
    )
  } finally {
    clearTimeout(timer)
  }
}
