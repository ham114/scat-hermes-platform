import { describe, expect, it } from 'vitest'
import { chartInput, defaultChart, resultChart } from './analysisChart'

describe('charting existing analysis tables', () => {
  it('aligns groups on the same category and preserves missing rows', () => {
    const input = chartInput({
      type: 'table',
      title: '机型比较',
      columns: [
        { key: 'aircraft', label: '机型' },
        { key: 'batch', label: '批次' },
        { key: 'flights', label: '航班数' },
      ],
      rows: [
        { aircraft: 'A320', batch: 'A', flights: 5 },
        { aircraft: 'A320', batch: 'B', flights: 2 },
        { aircraft: 'B738', batch: 'B', flights: 3 },
      ],
    })!
    expect(resultChart(input, { ...defaultChart(input, 'bar')!, group: 'batch' })).toMatchObject({
      xAxis: { data: ['A320', 'B738'] },
      series: [
        { name: 'A', data: [5, null] },
        { name: 'B', data: [2, 3] },
      ],
    })
  })

  it('keeps statistics with different units off a shared axis', () => {
    const input = chartInput({
      type: 'table',
      title: '通道分布',
      columns: [
        { key: 'unit', label: '单位' },
        { key: 'median', label: '中位数' },
        { key: 'valid', label: '有效值' },
      ],
      rows: [
        { unit: 'm', median: 300, valid: 5 },
        { unit: 'm/s', median: 2, valid: 4 },
      ],
    })!
    expect(input.fields.map((field) => field.key)).toEqual(['unit', 'valid'])
    expect(input.rows[0]?.median).toBe(300)
  })
})
