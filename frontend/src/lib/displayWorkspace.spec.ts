import { describe, it, expect } from 'vitest'
import fixture from '@/test-fixtures/analysis.json'
import { parseAnalysisArtifact } from './analysisDisplay'
import { defaultDisplay, displayCommand, displayBlocks, parseDisplay } from './displayWorkspace'
const result = parseAnalysisArtifact(fixture, fixture.artifact_id)!
describe('presentation changes', () => {
  it('combines layout, order, chart and visibility without modifying source', () => {
    const before = JSON.stringify(result)
    const c = displayCommand('双列；置顶 2；隐藏 3；柱状图', defaultDisplay(result), result)
    expect(c.columns).toBe(2)
    expect(c.order[0]).toBe(1)
    expect(c.chart).toBe('bar')
    expect(displayBlocks(result, c).some((x) => x.id === 2)).toBe(false)
    expect(JSON.stringify(result)).toBe(before)
  })
  it('rejects unsupported requests atomically, including calculations', () => {
    const c = defaultDisplay(result)
    expect(() => displayCommand('双列；重新训练', c, result)).toThrow()
    expect(c.columns).toBe(1)
  })
  it('rejects invalid saved configuration and unknown fields', () => {
    expect(() => parseDisplay({ ...defaultDisplay(result), order: [0, 0] }, result)).toThrow()
    expect(() => displayCommand('降序 2 不存在', defaultDisplay(result), result)).toThrow()
  })
  it('sorts numeric table values and filters columns on copies', () => {
    const table = result.blocks.find((x) => x.type === 'table')!
    if (table.type !== 'table') throw Error('fixture')
    const i = result.blocks.indexOf(table)
    const field = table.columns[0]!.key
    const c = displayCommand(
      `降序 ${i + 1} ${field}；只看 ${i + 1} ${field}`,
      defaultDisplay(result),
      result,
    )
    const projected = displayBlocks(result, c).find((x) => x.id === i)!.block
    expect(projected.type === 'table' && projected.columns.length).toBe(1)
    expect(projected).not.toBe(table)
    expect(displayCommand('恢复默认', c, result)).toEqual(defaultDisplay(result))
  })
})
