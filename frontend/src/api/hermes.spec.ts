import { afterEach, describe, expect, it, vi } from 'vitest'

import { createHermesRun, HermesApiError, newRequestId, readHermesEvents } from './hermes'

const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55'
const rid = 'run_test'
const requestId = 'fa7089b6-9a90-43c6-bbda-69bbf436973b'
function response(text: string) {
  const bytes = new TextEncoder().encode(text)
  return new Response(
    new ReadableStream({
      start(controller) {
        // Split within CJK codepoints as well as SSE framing.
        for (let i = 0; i < bytes.length; i += 2) controller.enqueue(bytes.slice(i, i + 2))
        controller.close()
      },
    }),
    { headers: { 'Content-Type': 'text/event-stream' } },
  )
}
function frame(event: string, sequence: number, fields = {}) {
  return `id: ${sequence}\r\ndata: ${JSON.stringify({ event, run_id: rid, ...fields })}\r\n\r\n`
}
afterEach(() => vi.unstubAllGlobals())

describe('Hermes HTTP and event client', () => {
  it('keeps the UUID and does not retry an ambiguous admission', async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError('private-network-error'))
    vi.stubGlobal('fetch', fetch)
    await expect(createHermesRun(tid, requestId, '你好')).rejects.toMatchObject({
      outcomeUnknown: true,
    })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(JSON.parse(fetch.mock.calls[0]![1].body)).toEqual({
      request_id: requestId,
      message: '你好',
    })
  })
  it('sanitizes upstream HTTP errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ error: { message: 'PRIVATE-KEY', outcome_unknown: false } }),
            { status: 429 },
          ),
        ),
    )
    await expect(createHermesRun(tid, requestId, '你好')).rejects.toThrow(HermesApiError)
    try {
      await createHermesRun(tid, requestId, '你好')
    } catch (error) {
      expect(String(error)).not.toContain('PRIVATE')
    }
  })
  it('decodes split UTF-8 and CRLF, deduplicates replay, and passes the cursor', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response(
          ': ping\r\n\r\n' +
            frame('message.delta', 0, { delta: 'old' }) +
            frame('message.delta', 1, { delta: '中文' }) +
            frame('message.delta', 1, { delta: 'duplicate' }) +
            frame('run.completed', 2, { output: '中文', completed: true }),
        ),
      )
    vi.stubGlobal('fetch', fetch)
    const received: string[] = []
    await readHermesEvents(
      tid,
      rid,
      (event) => received.push(event.delta ?? event.output ?? ''),
      new AbortController().signal,
      0,
    )
    expect(received).toEqual(['中文', '中文'])
    expect(fetch.mock.calls[0]![1].headers['Last-Event-ID']).toBe('0')
  })
  it('treats EOF as interrupted rather than complete', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(response(frame('message.delta', 0, { delta: 'partial' }))),
    )
    const events: string[] = []
    await expect(
      readHermesEvents(tid, rid, (event) => events.push(event.event), new AbortController().signal),
    ).rejects.toThrow('中断')
    expect(events).toEqual(['message.delta'])
  })
  it.each([
    frame('run.completed', 0, { completed: false, partial: true }),
    'id: wrong\ndata: ' + JSON.stringify({ event: 'run.completed', run_id: rid }) + '\n\n',
    'data: ' + JSON.stringify({ event: 'run.completed', run_id: 'run_other' }) + '\n\n',
    frame('stream.error', 0, { code: 'event_stream_interrupted' }),
  ])('rejects inconsistent or foreign event frames', async (text) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(text)))
    const onEvent = vi.fn()
    await expect(
      readHermesEvents(tid, rid, onEvent, new AbortController().signal),
    ).rejects.toThrow()
    expect(onEvent).not.toHaveBeenCalled()
  })
  it('creates valid UUIDs on HTTP LAN pages without randomUUID', () => {
    vi.stubGlobal('crypto', { getRandomValues: (value: Uint8Array) => value.fill(7) })
    expect(newRequestId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
  })
})
