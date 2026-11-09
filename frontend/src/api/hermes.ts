export interface HermesRun {
  run_id: string
  status: string
  output?: string
  completed?: boolean
  partial?: boolean
  interrupted?: boolean
  replayed?: boolean
}

export interface HermesEvent {
  event: string
  run_id: string
  sequence?: number
  delta?: string
  output?: string
  tool?: string
  tool_call_id?: string
  error?: boolean
  code?: string
  completed?: boolean
  partial?: boolean
  interrupted?: boolean
}

export class HermesApiError extends Error {
  readonly outcomeUnknown: boolean
  constructor(message: string, outcomeUnknown = false) {
    super(message)
    this.outcomeUnknown = outcomeUnknown
  }
}

export function newRequestId(): string {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID()
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 15) | 64
  bytes[8] = (bytes[8]! & 63) | 128
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

const base = '/api/v1/hermes/threads'
const runPath = (threadId: string, runId: string) =>
  `${base}/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}`

async function request<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal?.aborted) abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, 30000)
  try {
    const response = await fetch(path, {
      method: body ? 'POST' : 'GET',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Request-ID': newRequestId() },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      const unknown = Boolean(payload.error?.outcome_unknown ?? (body && response.status >= 500))
      const message =
        response.status === 409
          ? '当前会话或账号已有请求，或服务并发已满。请等待执行完成；连接中断时先恢复原请求。'
          : response.status === 404
            ? '未找到当前会话或运行。'
            : '助手暂时不可用，请稍后恢复本次请求。'
      throw new HermesApiError(message, unknown)
    }
    return (await response.json()) as T
  } catch (error) {
    if (error instanceof HermesApiError) throw error
    throw new HermesApiError('连接中断，本次请求号已保留，可恢复查询。', Boolean(body))
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}

export const createHermesThread = (signal?: AbortSignal) =>
  request<{ thread_id: string }>(base, {}, signal)
export const createHermesRun = (
  threadId: string,
  requestId: string,
  message: string,
  signal?: AbortSignal,
) =>
  request<HermesRun>(
    `${base}/${encodeURIComponent(threadId)}/runs`,
    { request_id: requestId, message },
    signal,
  )
export const getHermesRun = (threadId: string, runId: string, signal?: AbortSignal) =>
  request<HermesRun>(runPath(threadId, runId), undefined, signal)
export const stopHermesRun = (threadId: string, runId: string) =>
  request<HermesRun>(runPath(threadId, runId) + '/stop', {})

export async function readHermesEvents(
  threadId: string,
  runId: string,
  onEvent: (event: HermesEvent) => void,
  signal: AbortSignal,
  lastSequence = -1,
): Promise<void> {
  const response = await fetch(runPath(threadId, runId) + '/events', {
    credentials: 'same-origin',
    signal,
    headers: {
      Accept: 'text/event-stream',
      ...(lastSequence >= 0 ? { 'Last-Event-ID': String(lastSequence) } : {}),
    },
  })
  if (
    !response.ok ||
    !response.body ||
    !response.headers.get('content-type')?.startsWith('text/event-stream')
  ) {
    throw new HermesApiError('无法连接回答事件流，请恢复当前运行。')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let cursor = lastSequence
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) throw new HermesApiError('事件流中断，请恢复当前运行。')
      buffer += decoder.decode(chunk.value, { stream: true })
      // Normalize after joining chunks so a split CR/LF pair stays intact.
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        if (frame.length > 262144) throw new HermesApiError('事件超过显示上限，请查询运行状态。')
        const lines = frame.split('\n')
        const data = lines
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).replace(/^ /, ''))
          .join('\n')
        if (!data) continue
        const raw: unknown = JSON.parse(data)
        if (!raw || typeof raw !== 'object') throw new HermesApiError('收到无效事件。')
        const event = raw as HermesEvent
        if (event.run_id !== runId || typeof event.event !== 'string')
          throw new HermesApiError('收到不匹配的运行事件。')
        const id = lines
          .find((line) => line.startsWith('id:'))
          ?.slice(3)
          .trim()
        if (id !== undefined) {
          if (!/^\d+$/.test(id) || !Number.isSafeInteger(Number(id)))
            throw new HermesApiError('事件序号无效。')
          const sequence = Number(id)
          if (sequence <= cursor) continue
          cursor = sequence
          event.sequence = sequence
        }
        if (event.event === 'stream.error') throw new HermesApiError('事件流中断，请恢复当前运行。')
        if (
          event.event === 'run.completed' &&
          (event.completed === false || event.partial || event.interrupted)
        )
          throw new HermesApiError('运行尚未完整结束，请查询状态。')
        onEvent(event)
        if (
          ['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(event.event)
        )
          return
      }
      if (buffer.length > 262144) throw new HermesApiError('事件超过显示上限，请查询运行状态。')
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

export interface ModelTask {
  task_id: string
  run_id: string
  state: string
  snapshot_sha256: string
  expires_at: number
  parameters: Record<string, unknown>
  input_refs: Record<string, unknown>[]
  evaluator: Record<string, unknown> | null
  components: Record<string, unknown>
  code_version: string | null
  model: {
    display_name: string
    model_key: string
    version: string
    limits: Record<string, unknown>
  }
  error_code?: string | null
  execution_outcome_unknown?: boolean
  workflow?: {
    proposal: Record<string, unknown> & {
      kind: string
      models?: { model: string; version?: string }[]
    }
    gpu?: { device: string; release_id: string }
    coverage: { selected_count: number; candidate_count?: number }
    trials: { trial_id: string }[]
    synthetic: boolean
    tool_groups: Record<string, string[]>
    input_sha256: string
    prediction_contract?: {
      mode: string
      observation_last_n: number | null
      windows: {
        external_id: string
        point_count: number
        start_time: string
        forecast_origin: string
        span_s: number
        requested_as_of?: string
        last_observation_time?: string
        observation_lag_s?: number
        horizon_reference?: string
      }[]
    }
  }
  skill_job?: {
    state: string
    budget: Record<string, unknown>
    steps?: { operation: string; state: string; error?: string | null }[]
    backend_executions?: { id: string; state: string; calls: number }[]
  }
  run?: {
    status: string
    result_artifact_ids: string[]
    progress: number
    error?: { message: string } | null
  } | null
}
const taskPath = (threadId: string, taskId?: string) =>
  `${base}/${encodeURIComponent(threadId)}/model-tasks${taskId ? '/' + encodeURIComponent(taskId) : ''}`
export const listModelTasks = (threadId: string) =>
  request<{ items: ModelTask[] }>(taskPath(threadId))
export const confirmModelTask = (threadId: string, task: ModelTask) =>
  request<ModelTask>(taskPath(threadId, task.task_id) + '/confirm', {
    confirmed: true,
    snapshot_sha256: task.snapshot_sha256,
  })
export const revokeModelTask = (threadId: string, taskId: string) =>
  request<ModelTask>(taskPath(threadId, taskId) + '/revoke', {})
export const getModelTaskArtifact = (threadId: string, taskId: string, artifactId: string) =>
  request<Record<string, unknown>>(
    taskPath(threadId, taskId) + '/artifacts/' + encodeURIComponent(artifactId),
  )
export const getSavedModelOutput = (threadId: string, taskId: string) =>
  request<Record<string, unknown>>(taskPath(threadId, taskId) + '/saved-model-output')

export interface WorkflowCatalog {
  enabled: boolean
  synthetic: boolean
  groups: Record<string, string[]>
  control_group: Record<string, string[]>
  model_ids: string[]
  production_gpu: boolean
  models?: WorkflowModel[]
}
export interface WorkflowModel {
  model_id: string
  version: string
  execution: string
  constraints?: {
    min_observed_s?: number
    max_observation_points?: number
    max_horizon_s?: number
    forecast_horizon_multiple_s?: number
  }
}
export const getWorkflowCatalog = () => request<WorkflowCatalog>('/api/v1/hermes/workflow-catalog')
export const prepareWorkflow = (
  threadId: string,
  proposal: Record<string, unknown>,
  options?: { requestId?: string; budget?: Record<string, number> },
) =>
  request<ModelTask>(`${base}/${encodeURIComponent(threadId)}/workflow-tasks`, {
    request_id: options?.requestId ?? newRequestId(),
    proposal,
    budget: options?.budget,
  })
export const reconcileModelTask = (threadId: string, taskId: string) =>
  request<ModelTask>(taskPath(threadId, taskId) + '/reconcile', {})
export const cancelModelTask = (threadId: string, taskId: string) =>
  request<ModelTask>(taskPath(threadId, taskId) + '/cancel', {})
