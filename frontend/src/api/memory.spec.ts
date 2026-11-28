import { describe, expect, it, vi, afterEach } from 'vitest'
import { memoryRequest, memorySummary, readMemoryVersion } from './memory'
describe('memory control boundary', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('gets a validated version from the existing generic memory status', async () => {
    const fetcher = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        enabled: true,
        epoch: 2,
        chart_revision: 7,
        queue: {},
        deletion: 'none',
      }),
    })
    vi.stubGlobal('fetch', fetcher)
    expect(await readMemoryVersion()).toEqual({ enabled: true, epoch: 2, revision: 7 })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0]![0]).toBe('/api/v1/hermes/memory')
  })
  it('does not invent a memory version after a failed or incomplete read', async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(Error('offline'))
      .mockResolvedValue({ ok: true, json: async () => ({ enabled: true, epoch: 1 }) })
    vi.stubGlobal('fetch', fetcher)
    expect(await readMemoryVersion()).toBeUndefined()
    expect(await readMemoryVersion()).toBeUndefined()
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('does not portray a null representation as a formed memory', () => {
    expect(memorySummary('{"representation":null,"peer_card":null}')).toBe('')
    expect(memorySummary('{"representation":"合成偏好","peer_card":{}}')).toBe('合成偏好')
    expect(memorySummary('<script>untrusted</script>')).toBe('<script>untrusted</script>')
  })
  it('never retries a correction whose outcome is unknown', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('network'))
    vi.stubGlobal('fetch', fetcher)
    await expect(
      memoryRequest('/corrections', 'POST', { request_id: 'stable', content: 'synthetic' }),
    ).rejects.toThrow('待核实')
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0]![1].body).toContain('stable')
  })
})
