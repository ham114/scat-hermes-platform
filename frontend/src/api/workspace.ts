import { getHermesRun, HermesApiError, newRequestId, type HermesRun } from './hermes'
export interface HistoryThread {
  thread_id: string
  created_at: number
  title?: string | null
  title_revision?: number
}
export interface HistoryRun {
  run_id: string
  request_id: string
}
export interface ResultReference {
  trajectory_view_available?: boolean
  result_id: string
  operation: string
  payload_sha256: string
  created_at: number
  expires_at: number
  kind: string
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
export const validThread = (value: unknown): value is string =>
  typeof value === 'string' && uuid.test(value)
export const validRun = (value: unknown): value is string =>
  typeof value === 'string' && /^run_[A-Za-z0-9_-]{1,128}$/.test(value)
async function get<T>(path: string, signal?: AbortSignal, body?: object): Promise<T> {
  const control = new AbortController()
  const abort = () => control.abort()
  if (signal?.aborted) abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, 30000)
  try {
    const response = await fetch('/api/v1/hermes' + path, {
      method: body ? 'PATCH' : 'GET',
      body: body ? JSON.stringify(body) : undefined,
      signal: control.signal,
      credentials: 'same-origin',
      headers: {
        'X-Request-ID': newRequestId(),
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
    })
    if (!response.ok)
      throw new HermesApiError(
        response.status === 404 ? '记录不存在或已过期。' : '暂时无法读取，请重试。',
      )
    return (await response.json()) as T
  } catch (error) {
    if (error instanceof HermesApiError) throw error
    throw new HermesApiError('连接中断，请重试读取。')
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}
function threadPath(thread: string) {
  if (!validThread(thread)) throw new HermesApiError('会话编号无效。')
  return '/threads/' + encodeURIComponent(thread)
}
function runPath(thread: string, run: string) {
  if (!validRun(run)) throw new HermesApiError('运行编号无效。')
  return threadPath(thread) + '/runs/' + encodeURIComponent(run)
}
export async function listHistoryThreads(signal?: AbortSignal) {
  const page = await get<{ items: HistoryThread[] }>('/threads?limit=20', signal)
  if (
    !Array.isArray(page.items) ||
    page.items.length > 20 ||
    page.items.some((x) => !validThread(x?.thread_id) || !Number.isFinite(x.created_at))
  )
    throw new HermesApiError('会话列表无效。')
  return page.items
}
export async function loadHistoryRuns(
  thread: string,
  signal?: AbortSignal,
): Promise<(HermesRun & HistoryRun)[]> {
  const page = await get<{ items: HistoryRun[] }>(threadPath(thread) + '/runs?limit=20', signal)
  if (
    !Array.isArray(page.items) ||
    page.items.length > 20 ||
    page.items.some((x) => !validRun(x?.run_id) || !validThread(x?.request_id))
  )
    throw new HermesApiError('回复列表无效。')
  // Owner-scoped GETs only; never issue a new run while reading history.
  return Promise.all(
    [...page.items].reverse().map(async (item) => {
      const state = await getHermesRun(thread, item.run_id, signal)
      if (state.run_id !== item.run_id) throw new HermesApiError('回复归属不匹配。')
      return { ...item, ...state }
    }),
  )
}
function isReference(value: ResultReference) {
  return (
    value &&
    /^[a-f0-9]{32}$/.test(value.result_id) &&
    /^[a-f0-9]{64}$/.test(value.payload_sha256) &&
    typeof value.operation === 'string' &&
    Number.isFinite(value.created_at) &&
    Number.isFinite(value.expires_at) &&
    value.kind === 'readonly_tool_result'
  )
}
export async function listRunResults(thread: string, run: string, signal?: AbortSignal) {
  const page = await get<{ items: ResultReference[] }>(runPath(thread, run) + '/results', signal)
  if (!Array.isArray(page.items) || page.items.some((x) => !isReference(x)))
    throw new HermesApiError('结果引用无效。')
  return page.items
}
export async function readRunResult(
  thread: string,
  run: string,
  reference: ResultReference,
  signal?: AbortSignal,
) {
  if (!isReference(reference)) throw new HermesApiError('结果引用无效。')
  const value = await get<ResultReference & { result: unknown; analysis_result?: unknown }>(
    runPath(thread, run) + '/results/' + reference.result_id,
    signal,
  )
  if (
    !isReference(value) ||
    value.result_id !== reference.result_id ||
    value.operation !== reference.operation ||
    value.payload_sha256 !== reference.payload_sha256
  )
    throw new HermesApiError('结果引用不匹配。')
  return value
}

export interface TrajectoryViewCoverage {
  sampling_mode?: 'uniform' | 'segmented'
  total: number
  loaded: number
  every_nth: number
  complete: boolean
  full_extent: boolean
  partial: boolean
}
export async function readTrajectoryView(
  thread: string,
  run: string,
  reference: ResultReference,
  signal?: AbortSignal,
) {
  if (!isReference(reference) || reference.operation !== 'get_scat_track_points')
    throw new HermesApiError('轨迹来源无效。')
  const result = await get<{
    items: ResultReference[]
    coverage: TrajectoryViewCoverage
    message?: string
  }>(runPath(thread, run) + '/results/' + reference.result_id + '/trajectory', signal)
  if (
    !Array.isArray(result.items) ||
    result.items.length > 22 ||
    result.items.some((r) => !isReference(r) || r.operation !== 'get_scat_track_points') ||
    !result.coverage ||
    !Number.isSafeInteger(result.coverage.total) ||
    !Number.isSafeInteger(result.coverage.loaded) ||
    result.coverage.loaded < 0 ||
    result.coverage.loaded > 2000 ||
    result.coverage.loaded > result.coverage.total ||
    !Number.isSafeInteger(result.coverage.every_nth) ||
    result.coverage.every_nth < 1 ||
    ['complete', 'full_extent', 'partial'].some(
      (k) => typeof result.coverage[k as keyof TrajectoryViewCoverage] !== 'boolean',
    )
  )
    throw new HermesApiError('轨迹范围无效。')
  return result
}

export interface HistoryTurn {
  id: number
  request_id: string
  run_id: string | null
  input: string | null
  input_available: boolean
  output: string | null
  status: string
  created_at: number | null
  updated_at: number | null
  last_sequence: number
}
export interface HistoryPage<T> {
  items: T[]
  next_cursor: string | null
}
function pageCursor(value: unknown) {
  if (value !== null && value !== undefined && (typeof value !== 'string' || value.length > 512))
    throw new HermesApiError('分页信息无效。')
}
export async function listConversationThreads(
  after?: string,
  signal?: AbortSignal,
): Promise<HistoryPage<HistoryThread>> {
  const page = await get<HistoryPage<HistoryThread>>(
    '/threads?limit=20' + (after ? '&cursor=' + encodeURIComponent(after) : ''),
    signal,
  )
  pageCursor(page.next_cursor)
  if (
    !Array.isArray(page.items) ||
    page.items.length > 20 ||
    page.items.some(
      (x) =>
        !validThread(x?.thread_id) ||
        !Number.isFinite(x.created_at) ||
        (x.title != null && typeof x.title !== 'string') ||
        (x.title_revision !== undefined &&
          (!Number.isSafeInteger(x.title_revision) || x.title_revision < 0)),
    )
  )
    throw new HermesApiError('会话列表无效。')
  return { ...page, next_cursor: page.next_cursor ?? null }
}
export async function loadConversationPage(
  thread: string,
  before?: string,
  signal?: AbortSignal,
): Promise<HistoryPage<HistoryTurn>> {
  const page = await get<HistoryPage<HistoryTurn>>(
    threadPath(thread) +
      '/messages?limit=20' +
      (before ? '&cursor=' + encodeURIComponent(before) : ''),
    signal,
  )
  pageCursor(page.next_cursor)
  if (
    !Array.isArray(page.items) ||
    page.items.length > 20 ||
    page.items.some(
      (x) =>
        !x ||
        !Number.isSafeInteger(x.id) ||
        x.id < 1 ||
        !validThread(x.request_id) ||
        (x.run_id !== null && !validRun(x.run_id)) ||
        (x.input !== null && typeof x.input !== 'string') ||
        x.input_available !== (x.input !== null) ||
        (x.output !== null && typeof x.output !== 'string') ||
        ![
          'admitting',
          'started',
          'queued',
          'running',
          'completed',
          'failed',
          'cancelled',
          'interrupted',
          'unavailable',
        ].includes(x.status) ||
        !Number.isSafeInteger(x.last_sequence) ||
        x.last_sequence < -1,
    )
  )
    throw new HermesApiError('历史记录无效。')
  return { ...page, next_cursor: page.next_cursor ?? null }
}
export async function renameHistoryThread(thread: string, title: string, revision: number) {
  return get<{ thread_id: string; title: string; title_revision: number }>(
    threadPath(thread),
    undefined,
    { title, expected_revision: revision },
  )
}
