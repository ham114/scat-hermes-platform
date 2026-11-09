import { afterEach, describe, expect, it, vi } from 'vitest'
import { listHistoryThreads, loadHistoryRuns, listRunResults, readRunResult } from './workspace'
const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55'
afterEach(() => vi.unstubAllGlobals())
describe('Owner-scoped workspace reads', () => {
  it('reads threads and existing runs using GET only', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [{ thread_id: tid, created_at: 1 }] })),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [{ run_id: 'run_old', request_id: tid }] })),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ run_id: 'run_old', status: 'completed', output: '真实回复' }),
        ),
      )
    vi.stubGlobal('fetch', fetch)
    expect((await listHistoryThreads())[0]?.thread_id).toBe(tid)
    expect((await loadHistoryRuns(tid))[0]?.output).toBe('真实回复')
    expect(
      fetch.mock.calls.every((call) => (call[1]?.method ?? 'GET') === 'GET' && !call[1]?.body),
    ).toBe(true)
  })
  it('rejects malformed history identifiers before reading unrelated paths', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    await expect(loadHistoryRuns('../other')).rejects.toThrow('会话编号')
    expect(fetch).not.toHaveBeenCalled()
  })
  it('rejects mismatched run ownership and result digests', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [{ run_id: 'run_old', request_id: tid }] })),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ run_id: 'run_other', status: 'completed' })),
      )
    vi.stubGlobal('fetch', fetch)
    await expect(loadHistoryRuns(tid)).rejects.toThrow('归属不匹配')
    const reference = {
      result_id: 'a'.repeat(32),
      payload_sha256: 'b'.repeat(64),
      created_at: 1,
      expires_at: 9999999999,
      operation: 'search_scat_flights',
      kind: 'readonly_tool_result',
    }
    fetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ ...reference, payload_sha256: 'c'.repeat(64), result: {} })),
    )
    await expect(readRunResult(tid, 'run_old', reference)).rejects.toThrow('不匹配')
  })
  it('rejects malformed references rather than linking arbitrary sources', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ items: [{ result_id: 'https://outside' }] })),
        ),
    )
    await expect(listRunResults(tid, 'run_old')).rejects.toThrow('引用无效')
  })
})

describe('Conversation pages', () => {
  it('keeps original questions and validates cursor pages', async () => {
    const turn = {
      id: 1,
      request_id: tid,
      run_id: 'run_one',
      input: '原始提问',
      input_available: true,
      output: '实际回复',
      status: 'completed',
      created_at: 1,
      updated_at: 2,
      last_sequence: 3,
    }
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [turn], next_cursor: 'older' })))
    vi.stubGlobal('fetch', fetch)
    const { loadConversationPage } = await import('./workspace')
    const page = await loadConversationPage(tid, 'before')
    expect(page.items[0]?.input).toBe('原始提问')
    expect(page.next_cursor).toBe('older')
    expect(fetch.mock.calls[0]![0]).toContain('cursor=before')
    expect(fetch.mock.calls[0]![1].method).toBe('GET')
  })
  it('does not invent a question for legacy history', async () => {
    const turn = {
      id: 1,
      request_id: tid,
      run_id: 'run_old',
      input: null,
      input_available: false,
      output: '旧回复',
      status: 'completed',
      created_at: null,
      updated_at: 2,
      last_sequence: -1,
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [turn], next_cursor: null }))),
    )
    const { loadConversationPage } = await import('./workspace')
    expect((await loadConversationPage(tid)).items[0]?.input).toBeNull()
  })
})
