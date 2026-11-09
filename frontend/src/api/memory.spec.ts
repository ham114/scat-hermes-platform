import { describe, expect, it, vi, afterEach } from 'vitest'
import { memoryRequest, memorySummary } from './memory'
describe('memory control boundary', () => {
  afterEach(() => vi.unstubAllGlobals())
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
