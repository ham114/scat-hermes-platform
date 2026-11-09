import createClient from 'openapi-fetch'

import type { components, operations, paths } from './schema'
import { ScatApiError, toHttpError } from './errors'

export type BatchPage = components['schemas']['BatchPage']
export type BatchOverview = components['schemas']['BatchOverview']
export type BatchSummary = components['schemas']['BatchSummary']
export type BatchDataQuality = components['schemas']['BatchDataQuality']
export type FlightVolumePoint = components['schemas']['FlightVolumePoint']
export type FlightPage = components['schemas']['FlightPage']
export type FlightOverview = components['schemas']['FlightOverview']
export type TrajectoryPage = components['schemas']['TrajectoryPage']
export type FplOverview = components['schemas']['FplOverview']
export type PredictionSnapshotPage = components['schemas']['PredictionSnapshotPage']
export type PredictionSnapshot = components['schemas']['PredictionSnapshot']
export type AnalysisArtifactView = components['schemas']['AnalysisArtifactView']
export type AnalysisArtifactSummary = components['schemas']['AnalysisArtifactSummary']
export type AnalysisArtifactSummaryPage = components['schemas']['AnalysisArtifactSummaryPage']

export type BatchListQuery = NonNullable<operations['list_batches']['parameters']['query']>
export type FlightSearchQuery = NonNullable<operations['search_flights']['parameters']['query']>

export interface RequestOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

interface FetchResult<T> {
  data?: T
  error?: unknown
  response: Response
}

interface RequestContext {
  signal: AbortSignal
  requestId: string
}

const DEFAULT_TIMEOUT_MS = 10_000
const api = createClient<paths>({
  // Absolute same-origin URLs also work in Node-based unit tests.
  baseUrl: globalThis.location?.origin ?? 'http://localhost',
  // Resolve fetch at call time so tests and host applications can safely replace it.
  fetch: (...args) => globalThis.fetch(...args),
})

function createRequestId() {
  return (
    globalThis.crypto?.randomUUID?.() ?? `web-${Date.now()}-${Math.random().toString(16).slice(2)}`
  )
}

function createControlledSignal(options: RequestOptions) {
  const controller = new AbortController()
  let didTimeout = false

  const abortFromCaller = () => controller.abort(options.signal?.reason)
  if (options.signal?.aborted) abortFromCaller()
  else options.signal?.addEventListener('abort', abortFromCaller, { once: true })

  const timer = globalThis.setTimeout(() => {
    didTimeout = true
    controller.abort(new DOMException('Request timeout', 'TimeoutError'))
  }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS)

  return {
    signal: controller.signal,
    didTimeout: () => didTimeout,
    cleanup: () => {
      globalThis.clearTimeout(timer)
      options.signal?.removeEventListener('abort', abortFromCaller)
    },
  }
}

async function request<T>(
  execute: (context: RequestContext) => Promise<FetchResult<T>>,
  options: RequestOptions = {},
): Promise<T> {
  const controlled = createControlledSignal(options)
  const requestId = createRequestId()

  try {
    const result = await execute({ signal: controlled.signal, requestId })
    if (result.data !== undefined) return result.data
    if (result.error !== undefined) throw toHttpError(result.error, result.response, requestId)

    throw new ScatApiError({
      kind: 'invalid-response',
      message: '接口没有返回符合契约的数据。',
      requestId: result.response.headers.get('x-request-id') ?? requestId,
    })
  } catch (error) {
    if (error instanceof ScatApiError) throw error
    if (controlled.signal.aborted) {
      throw new ScatApiError({
        kind: controlled.didTimeout() ? 'timeout' : 'cancelled',
        message: controlled.didTimeout() ? '接口请求超时。' : '接口请求已取消。',
        requestId,
        cause: error,
      })
    }
    if (error instanceof SyntaxError) {
      throw new ScatApiError({
        kind: 'invalid-response',
        message: '后端返回了无法解析的响应。',
        requestId,
        cause: error,
      })
    }
    throw new ScatApiError({
      kind: 'network',
      message: '无法连接到后端服务。',
      requestId,
      cause: error,
    })
  } finally {
    controlled.cleanup()
  }
}

function requestHeaders(requestId: string) {
  return { Accept: 'application/json', 'X-Request-ID': requestId }
}

export function getAnalysisArtifact(artifactId: string, options?: RequestOptions) {
  return request<AnalysisArtifactView>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/analysis/artifacts/{artifact_id}', {
        params: { path: { artifact_id: artifactId } },
        headers: requestHeaders(requestId),
        credentials: 'same-origin',
        signal,
      }),
    options,
  )
}

export function listAnalysisArtifacts(
  query: NonNullable<operations['list_analysis_artifact_summaries']['parameters']['query']> = {},
  options?: RequestOptions,
) {
  return request<AnalysisArtifactSummaryPage>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/analysis/artifacts', {
        params: { query },
        headers: requestHeaders(requestId),
        credentials: 'same-origin',
        signal,
      }),
    options,
  )
}

export function listBatches(query: BatchListQuery = {}, options?: RequestOptions) {
  return request<BatchPage>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/batches', {
        params: { query },
        headers: requestHeaders(requestId),
        signal,
      }),
    options,
  )
}

export function getBatchOverview(
  batch: string,
  distributionLimit = 10,
  timeGranularity: 'day' | 'hour' = 'day',
  options?: RequestOptions,
) {
  return request<BatchOverview>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/batches/{batch}/overview', {
        params: {
          path: { batch },
          query: {
            distribution_limit: distributionLimit,
            time_granularity: timeGranularity,
          },
        },
        headers: requestHeaders(requestId),
        signal,
      }),
    options,
  )
}

export function searchFlights(query: FlightSearchQuery = {}, options?: RequestOptions) {
  return request<FlightPage>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/flights', {
        params: { query },
        headers: requestHeaders(requestId),
        signal,
      }),
    options,
  )
}

export function getFlight(batch: string, externalId: string, options?: RequestOptions) {
  return request<FlightOverview>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/flights/{batch}/{external_id}', {
        params: { path: { batch, external_id: externalId } },
        headers: requestHeaders(requestId),
        signal,
      }),
    options,
  )
}

export function getTrajectory(
  batch: string,
  externalId: string,
  query: NonNullable<operations['get_trajectory']['parameters']['query']> = {},
  options?: RequestOptions,
) {
  return request<TrajectoryPage>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/flights/{batch}/{external_id}/trajectory', {
        params: { path: { batch, external_id: externalId }, query },
        headers: requestHeaders(requestId),
        signal,
      }),
    options,
  )
}

export function getFpl(batch: string, externalId: string, options?: RequestOptions) {
  return request<FplOverview>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/flights/{batch}/{external_id}/fpl', {
        params: { path: { batch, external_id: externalId } },
        headers: requestHeaders(requestId),
        signal,
      }),
    options,
  )
}

export function listPredictions(
  batch: string,
  externalId: string,
  query: NonNullable<operations['list_predictions']['parameters']['query']> = {},
  options?: RequestOptions,
) {
  return request<PredictionSnapshotPage>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/flights/{batch}/{external_id}/predictions', {
        params: { path: { batch, external_id: externalId }, query },
        headers: requestHeaders(requestId),
        signal,
      }),
    options,
  )
}

export function getPredictionSnapshot(
  batch: string,
  externalId: string,
  snapshotTime: string,
  options?: RequestOptions,
) {
  return request<PredictionSnapshot>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/flights/{batch}/{external_id}/prediction-snapshots/{snapshot_time}', {
        params: {
          path: { batch, external_id: externalId, snapshot_time: snapshotTime },
        },
        headers: requestHeaders(requestId),
        signal,
      }),
    options,
  )
}

// M5-08 评估页数据出口：两条读路由（服务端未声明 response_model，响应类型按服务层契约声明）。
export type MetricSummaryQuery = NonNullable<
  operations['get_metric_summaries']['parameters']['query']
>
export type WorstPredictionQuery = NonNullable<
  operations['get_worst_prediction_samples']['parameters']['query']
>

// M6-09 评估页数据出口：自研模型实验台账（scat_model_experiments，口径 m6-eval-v1）。
export type ModelExperimentQuery = NonNullable<
  operations['list_model_experiments']['parameters']['query']
>

/** 预测评估分组维度（与后端 group_by 取值一致）。 */
export type MetricGroupBy = 'overall' | 'phase' | 'aircraft_type'

/** 最差案例排序字段（与后端 metric 取值一致）。 */
export type WorstPredictionMetric = 'horizontal' | 'vertical' | 'along' | 'cross'

/** scat_prediction_metric_summaries 的一行（MAE/RMSE 与 overall 分位数）。 */
export interface MetricSummaryRow {
  batch: string
  horizon_s: number
  group_by: string
  group_value: string
  sample_count: number
  flight_count: number
  horizontal_mae_m: number | null
  horizontal_rmse_m: number | null
  vertical_mae_m: number | null
  vertical_rmse_m: number | null
  along_track_mae_m: number | null
  cross_track_mae_m: number | null
  horizontal_median_m: number | null
  horizontal_p90_m: number | null
  horizontal_p95_m: number | null
  vertical_median_m: number | null
  vertical_p90_m: number | null
  vertical_p95_m: number | null
  percentile_method: string | null
  metric_version: string
  algorithm_version: string
  source_version: string
  generated_at: string
}

export interface MetricSummaryPage {
  batch: string
  group_by: string | null
  horizon_s: number | null
  record_count: number
  summaries: MetricSummaryRow[]
}

/** scat_prediction_metrics 的一条最差样本（附航班主导阶段与机型）。 */
export interface WorstPredictionSample {
  batch: string
  external_id: string
  snapshot_time: string
  horizon_s: number
  horizontal_error_m: number | null
  vertical_error_m: number | null
  along_track_error_m: number | null
  cross_track_error_m: number | null
  is_valid: boolean
  dominant_phase: string | null
  aircraft_type: string | null
}

export interface WorstPredictionSamplePage {
  batch: string
  metric: string
  horizon_s: number | null
  sample_count: number
  samples: WorstPredictionSample[]
}

/** 自研模型实验的一条时域指标（m6-eval-v1，单位米）。 */
export interface ModelExperimentHorizonMetric {
  horizon_s: number
  sample_count: number
  flight_count: number
  mae_m: number | null
  rmse_m: number | null
  median_m: number | null
  p90_m: number | null
  p95_m: number | null
}

/** scat_model_experiments 的一条实验记录（评估页对比所需字段）。 */
export interface ModelExperimentSummary {
  experiment_id: string
  kind: string
  status: string
  batch: string
  predictor_id: string
  predictor_version: string
  data_version: string
  feature_version?: string | null
  protocol_version: string
  source?: string | null
  created_at: string
  finished_at: string | null
  dataset?: {
    selection?: string
    limit?: number | null
    flight_count?: number
    external_ids_sha256?: string | null
    sample_flight_ids?: string[]
  } | null
  coverage?: Record<string, unknown> | null
  metrics?: ModelExperimentHorizonMetric[] | null
}

export interface ModelExperimentPage {
  batch: string | null
  predictor_id: string | null
  count: number
  experiments: ModelExperimentSummary[]
}

export function listMetricSummaries(
  batch: string,
  query: MetricSummaryQuery = {},
  options?: RequestOptions,
) {
  return request<MetricSummaryPage>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/materialization/batches/{batch}/metric-summaries', {
        params: { path: { batch }, query },
        headers: requestHeaders(requestId),
        signal,
      }) as Promise<FetchResult<MetricSummaryPage>>,
    options,
  )
}

export function listWorstPredictionSamples(
  batch: string,
  query: WorstPredictionQuery = {},
  options?: RequestOptions,
) {
  return request<WorstPredictionSamplePage>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/materialization/batches/{batch}/worst-prediction-samples', {
        params: { path: { batch }, query },
        headers: requestHeaders(requestId),
        signal,
      }) as Promise<FetchResult<WorstPredictionSamplePage>>,
    options,
  )
}

export function listModelExperiments(query: ModelExperimentQuery = {}, options?: RequestOptions) {
  return request<ModelExperimentPage>(
    ({ signal, requestId }) =>
      api.GET('/api/v1/scat/experiments/model-experiments', {
        params: { query },
        headers: requestHeaders(requestId),
        signal,
      }) as Promise<FetchResult<ModelExperimentPage>>,
    options,
  )
}
