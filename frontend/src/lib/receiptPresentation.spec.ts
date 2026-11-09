import { describe, expect, it } from 'vitest'
import { presentReceipts, receiptRange, type ReceiptView } from './receiptPresentation'
import { parseAnalysisArtifact } from './analysisDisplay'
import fixture from '@/test-fixtures/readonly-track.json'

function source(start: number, step: number, count: number, consumed: number): ReceiptView {
  const analysis = parseAnalysisArtifact(
    { artifact_id: fixture.result_id, analysis_result: structuredClone(fixture.analysis_result) },
    fixture.result_id,
  )!
  const block = analysis.blocks.find((b) => b.type === 'trajectory_2d')!
  if (block.type !== 'trajectory_2d') throw new Error('fixture')
  delete block.time_key
  block.points = Array.from({ length: count }, (_, i) => ({
    longitude: 4 + (start + i * step) / 1000,
    latitude: 50,
    altitude: 1000,
  }))
  analysis.blocks = [block]
  return {
    reference: {
      result_id: String(start).padStart(32, '0'),
      payload_sha256: 'b'.repeat(64),
      operation: 'get_scat_track_points',
      created_at: start,
      expires_at: 9999999999,
      kind: 'readonly_tool_result',
    },
    data: {
      query: { batch: 'SYNTH', external_id: '100002', start, every_nth: step },
      data: {
        pagination: { start, source_points_consumed: consumed, total_points: 10 },
        points: Array.from({ length: count }, () => ({})),
      },
    },
    analysis,
  }
}
describe('one result from verified receipt pages', () => {
  it('combines pages in source order and deduplicates an overlapping tail without claiming full coverage', () => {
    const sources = [source(0, 2, 3, 6), source(6, 2, 2, 4), source(8, 1, 2, 2)]
    const original = JSON.stringify(sources)
    const result = presentReceipts(sources)
    expect(result).toHaveLength(1)
    expect(result[0]!.note).toContain('去重1点；已加载6/10点')
    expect(result[0]!.note).toContain('不代表完整逐点航迹')
    expect(result[0]!.analysis.blocks.find((b) => b.type === 'trajectory_2d')?.points).toHaveLength(
      6,
    )
    expect(result[0]!.analysis.context?.readonly_payload_sha256).toBeUndefined()
    expect(JSON.stringify(sources)).toBe(original)
    expect(receiptRange(sources[1]!)).toContain('源序号 6–9 · 2点 · 每2点抽1点')
  })
  it('recognizes complete consecutive coverage', () => {
    const a = source(0, 1, 6, 6),
      b = source(6, 1, 4, 4)
    Object.assign((a.data as { query: object }).query, { fields: ['position', 'motion'] })
    Object.assign((b.data as { query: object }).query, { fields: ['position'] })
    expect(presentReceipts([a, b])[0]!.note).toContain('已覆盖完整源序列')
  })
  it('keeps conflicting overlaps and different flight scopes separate', () => {
    const a = source(0, 1, 6, 6),
      b = source(4, 1, 6, 6)
    const block = b.analysis!.blocks[0]!
    if (block.type === 'trajectory_2d') block.points[0]!.longitude = 10
    expect(presentReceipts([a, b])).toHaveLength(2)
    const c = source(6, 1, 4, 4)
    ;(c.data as { query: { external_id: string } }).query.external_id = '100200'
    expect(presentReceipts([a, c])).toHaveLength(2)
  })
  it('never aligns independent page-relative time origins', () => {
    const a = source(0, 1, 6, 6),
      b = source(6, 1, 4, 4)
    for (const item of [a, b]) {
      const block = item.analysis!.blocks[0]!
      if (block.type === 'trajectory_2d') block.time_key = 'time_step_s'
    }
    a.analysis!.start_time = '2020-01-01T00:00:00Z'
    b.analysis!.start_time = '2020-01-01T00:01:00Z'
    expect(presentReceipts([a, b])).toHaveLength(2)
  })
})
