import { describe, expect, it, vi } from 'vitest'

import {
  getAnalysisArtifact,
  listAnalysisArtifacts,
  listBatches,
  listModelExperiments,
} from './client'
import { ScatApiError } from './errors'

describe('SCAT API client', () => {
  it('loads an owner-scoped analysis artifact without sending identity parameters', async () => {
    const fetchMock = vi.fn(async (request: Request) => {
      const url = new URL(request.url)
      expect(url.pathname).toBe(`/api/v1/analysis/artifacts/${'a'.repeat(32)}`)
      expect(url.search).toBe('')
      expect(request.credentials).toBe('same-origin')
      return new Response(
        JSON.stringify({
          schema_version: '1.0',
          artifact_id: 'a'.repeat(32),
          run_id: 'b'.repeat(32),
          kind: 'analysis_result',
          thread_id: 'thread-1',
          model_key: 'scat-gbar-v23',
          model_version: '23',
          analysis_result: {
            schema_version: '1.4',
            dataset: 'scat',
            operation: 'run_regression_model',
            title: 'Prediction',
            context: {},
            blocks: [{ type: 'metrics', title: 'Summary', items: [] }],
          },
          created_at: '2026-09-16T08:00:00Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await getAnalysisArtifact('a'.repeat(32))

    expect(result.thread_id).toBe('thread-1')
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('lists lightweight owner-scoped comparison artifacts', async () => {
    const fetchMock = vi.fn(async (request: Request) => {
      const url = new URL(request.url)
      expect(url.pathname).toBe('/api/v1/analysis/artifacts')
      expect(url.searchParams.get('operation')).toBe('run_model_comparison')
      expect(url.searchParams.get('batch')).toBe('scat20161015_20161021')
      expect(request.credentials).toBe('same-origin')
      return new Response(JSON.stringify({ count: 0, artifacts: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await listAnalysisArtifacts({
      operation: 'run_model_comparison',
      batch: 'scat20161015_20161021',
      limit: 5,
    })

    expect(result.count).toBe(0)
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('returns a typed batch page and sends a request id', async () => {
    const fetchMock = vi.fn(async (request: Request) => {
      expect(request.url).toContain('/api/v1/scat/batches?limit=1')
      expect(request.headers.get('x-request-id')).toBeTruthy()
      return new Response(
        JSON.stringify({
          pagination: { offset: 0, limit: 1, returned: 0, total: 0, has_more: false },
          items: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await listBatches({ limit: 1 })

    expect(result.pagination.total).toBe(0)
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('requests self-developed model experiments for a batch', async () => {
    const fetchMock = vi.fn(async (request: Request) => {
      const url = new URL(request.url)
      expect(url.pathname).toBe('/api/v1/scat/experiments/model-experiments')
      expect(url.searchParams.get('batch')).toBe('scat20161015_20161021')
      expect(url.searchParams.get('limit')).toBe('20')
      return new Response(
        JSON.stringify({
          batch: 'scat20161015_20161021',
          predictor_id: null,
          count: 0,
          experiments: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await listModelExperiments({ batch: 'scat20161015_20161021', limit: 20 })

    expect(result.count).toBe(0)
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('preserves backend error code and request id', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: {
                code: 'SCAT_DATABASE_UNAVAILABLE',
                message: 'MongoDB is unavailable.',
                request_id: 'req-test-503',
              },
            }),
            {
              status: 503,
              headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'req-test-503' },
            },
          ),
      ),
    )

    await expect(listBatches()).rejects.toMatchObject({
      kind: 'http',
      status: 503,
      code: 'SCAT_DATABASE_UNAVAILABLE',
      requestId: 'req-test-503',
      retryable: true,
    })
  })

  it('distinguishes timeout from caller cancellation', async () => {
    vi.useFakeTimers()
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (request: Request) =>
          new Promise<Response>((_, reject) => {
            request.signal.addEventListener('abort', () => reject(request.signal.reason))
          }),
      ),
    )

    const pending = listBatches({}, { timeoutMs: 25 }).catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(25)
    const error = await pending

    expect(error).toBeInstanceOf(ScatApiError)
    expect(error).toMatchObject({ kind: 'timeout' })
  })

  it('recognizes an explicit caller cancellation', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (request: Request) =>
          new Promise<Response>((_, reject) => {
            request.signal.addEventListener('abort', () => reject(request.signal.reason))
          }),
      ),
    )
    const controller = new AbortController()
    const pending = listBatches({}, { signal: controller.signal }).catch((error: unknown) => error)

    controller.abort()

    await expect(pending).resolves.toMatchObject({ kind: 'cancelled' })
  })

  it('classifies malformed JSON as an invalid response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('{broken', {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
      ),
    )

    await expect(listBatches()).rejects.toMatchObject({ kind: 'invalid-response' })
  })

  it('classifies transport failures as network errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new TypeError('connection refused'))),
    )

    await expect(listBatches()).rejects.toMatchObject({ kind: 'network' })
  })
})
