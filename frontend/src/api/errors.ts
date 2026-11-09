export type ApiErrorKind = 'http' | 'network' | 'timeout' | 'cancelled' | 'invalid-response'

export interface ApiErrorOptions {
  kind: ApiErrorKind
  message: string
  status?: number
  code?: string
  requestId?: string
  details?: unknown
  cause?: unknown
}

export class ScatApiError extends Error {
  readonly kind: ApiErrorKind
  readonly status?: number
  readonly code?: string
  readonly requestId?: string
  readonly details?: unknown

  constructor(options: ApiErrorOptions) {
    super(options.message, { cause: options.cause })
    this.name = 'ScatApiError'
    this.kind = options.kind
    this.status = options.status
    this.code = options.code
    this.requestId = options.requestId
    this.details = options.details
  }

  get retryable() {
    return this.kind === 'network' || this.kind === 'timeout' || (this.status ?? 0) >= 500
  }
}

interface ErrorEnvelope {
  error?: {
    code?: unknown
    message?: unknown
    request_id?: unknown
    details?: unknown
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

export function toHttpError(payload: unknown, response: Response, fallbackRequestId: string) {
  const envelope = isRecord(payload) ? (payload as ErrorEnvelope) : undefined
  const detail = envelope?.error
  const requestId =
    (typeof detail?.request_id === 'string' && detail.request_id) ||
    response.headers.get('x-request-id') ||
    fallbackRequestId

  return new ScatApiError({
    kind: 'http',
    status: response.status,
    code: typeof detail?.code === 'string' ? detail.code : `HTTP_${response.status}`,
    message:
      typeof detail?.message === 'string'
        ? detail.message
        : `接口请求失败（HTTP ${response.status}）`,
    requestId,
    details: detail?.details,
  })
}

export function toDisplayMessage(error: unknown) {
  if (!(error instanceof ScatApiError)) return '发生未知错误，请稍后重试。'

  switch (error.kind) {
    case 'timeout':
      return '请求超时，请检查网络或稍后重试。'
    case 'cancelled':
      return '请求已取消。'
    case 'network':
      return '无法连接到后端服务，请检查开发代理或服务状态。'
    case 'invalid-response':
      return '后端响应格式不符合接口契约。'
    default:
      return error.message
  }
}
