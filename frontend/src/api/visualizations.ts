import type { components } from './schema'
import { HermesApiError, newRequestId } from './hermes'
import { memoryRequest, type ChartPreferences } from './memory'

export async function chartMemoryToken(text: string) {
  if (!/以后|今后|所有图|长期|请记住|记住我的|always\s+use|remember\s+my/i.test(text))
    return undefined
  try {
    const value = await memoryRequest<ChartPreferences>('/chart-preferences')
    if (Number.isSafeInteger(value.epoch) && Number.isSafeInteger(value.revision)) return value
  } catch {
    /* Local chart feedback remains usable without configured memory. */
  }
  return undefined
}

export type VisualizationTask = components['schemas']['VisualizationTaskView']
export type VisualizationArtifact = components['schemas']['VisualizationArtifactView']
export type DataReference = components['schemas']['SourceReference']
export type VisualizationSource = DataReference & { operation: string }
const base = (thread: string) => `/api/v1/hermes/threads/${encodeURIComponent(thread)}`

async function request<T>(url: string, body?: object, signal?: AbortSignal): Promise<T> {
  const control = new AbortController()
  const abort = () => control.abort()
  if (signal?.aborted) abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, 30000)
  try {
    const response = await fetch(url, {
      method: body ? 'POST' : 'GET',
      credentials: 'same-origin',
      signal: control.signal,
      headers: { 'Content-Type': 'application/json', 'X-Request-ID': newRequestId() },
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!response.ok)
      throw new HermesApiError(
        response.status === 404
          ? '图表或来源不可访问。'
          : response.status === 409
            ? '请求未被接受，请检查数据范围、功能开关与剩余额度。'
            : '图表服务暂时不可用。',
        Boolean(body && response.status >= 500),
      )
    return (await response.json()) as T
  } catch (error) {
    if (error instanceof HermesApiError) throw error
    throw new HermesApiError('连接中断，请查询已有任务或恢复原请求。', Boolean(body))
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}
export const listVisualizations = (thread: string, signal?: AbortSignal) =>
  request<components['schemas']['VisualizationTaskList']>(
    base(thread) + '/visualization-tasks',
    undefined,
    signal,
  )
export const visualizationSources = (thread: string, signal?: AbortSignal) =>
  request<{ items: VisualizationSource[] }>(
    base(thread) + '/visualization-sources',
    undefined,
    signal,
  )
export const createVisualization = (
  thread: string,
  body: components['schemas']['CreateVisualization'],
) => request<VisualizationTask>(base(thread) + '/visualizations', body)
export const reviseVisualization = (
  thread: string,
  artifact: string,
  body: components['schemas']['ReviseVisualization'],
) =>
  request<VisualizationTask>(
    base(thread) + `/visualizations/${encodeURIComponent(artifact)}/revisions`,
    body,
  )
export const cancelVisualization = (thread: string, task: string) =>
  request<VisualizationTask>(
    base(thread) + `/visualization-tasks/${encodeURIComponent(task)}/cancel`,
    {},
  )
export const readVisualization = (thread: string, artifact: string, signal?: AbortSignal) =>
  request<VisualizationArtifact>(
    base(thread) + `/visualizations/${encodeURIComponent(artifact)}`,
    undefined,
    signal,
  )
export const visualizationFile = (thread: string, artifact: string, file: string) =>
  base(thread) + `/visualizations/${encodeURIComponent(artifact)}/files/${encodeURIComponent(file)}`
