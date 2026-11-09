import { describe, expect, it } from 'vitest'
import fixture from '@/test-fixtures/analysis.json'
import {
  blockChart,
  parseAnalysisArtifact,
  safeAnalysisLink,
  scalarLabel,
  trajectoryChart,
} from './analysisDisplay'

const result = () => parseAnalysisArtifact(structuredClone(fixture), fixture.artifact_id)!

describe('analysis result values and display boundaries', () => {
  it('accepts the versioned seven-block artifact without changing its values', () => {
    expect(result()).toEqual(fixture.analysis_result)
    expect(result().blocks).toHaveLength(7)
    expect(parseAnalysisArtifact(fixture, 'another-artifact')).toBeNull()
  })
  it('preserves gaps and units rather than turning missing observations into zero', () => {
    const block = result().blocks.find((b) => b.type === 'timeseries')!
    const option = blockChart(block) as { series: { name: string; data: unknown[] }[] }
    expect(option.series[0]!.name).toBe('速度（m/s）')
    expect(option.series[0]!.data).toEqual([
      [0, 100],
      [10, null],
      [20, 110],
    ])
    expect(scalarLabel(null)).toBe('未提供')
    expect(scalarLabel(0)).toBe('0')
  })
  it('separates prediction snapshots, path roles and segments', () => {
    const block = result().blocks.find((b) => b.type === 'trajectory_2d')!
    if (block.type !== 'trajectory_2d') throw new Error('missing fixture')
    const first = trajectoryChart(block, '时刻A') as { series: { name: string; data: unknown[] }[] }
    const second = trajectoryChart(block, '时刻B') as typeof first
    expect(first.series.map((s) => s.name)).toEqual(['观测 · 连续段1', '预测 · 未来段'])
    expect(first.series[1]!.data).toEqual([
      [10, 5],
      [20, 12],
    ])
    expect(second.series.map((s) => s.name)).toEqual(['观测 · 连续段2'])
  })
  it('preserves missing prediction bounds and exact classification counts', () => {
    const prediction = blockChart(result().blocks.find((b) => b.type === 'prediction')!) as {
      series: { name: string; data: unknown[] }[]
    }
    expect(prediction.series.find((s) => s.name === '下界')!.data).toEqual([
      [10, 1030],
      [20, null],
    ])
    const classes = blockChart(
      result().blocks.find((b) => b.type === 'classification')!,
    ) as typeof prediction
    expect(classes.series[0]!.data).toEqual([2, 1])
  })
  it.each([
    'javascript:alert(1)',
    '//evil.invalid',
    '/\\evil.invalid',
    '/%2fevil.invalid',
    'https://user:password@example.com/',
    '/assistant?api_key=secret',
    '/assistant%0a',
    'data:text/html,test',
  ])('rejects unsafe action URL %s', (url) => {
    expect(safeAnalysisLink(url, 'https://platform.example')).toBeNull()
  })
  it('keeps same-origin links and explicit public HTTPS links', () => {
    expect(safeAnalysisLink('/assistant?thread_id=one', 'https://platform.example')).toBe(
      '/assistant?thread_id=one',
    )
    expect(safeAnalysisLink('https://docs.example/path', 'https://platform.example')).toBe(
      'https://docs.example/path',
    )
  })
  it('refuses malformed, unknown and oversized display payloads', () => {
    const bad = structuredClone(fixture)
    bad.analysis_result.blocks = [
      { type: 'unknown' },
    ] as unknown as typeof bad.analysis_result.blocks
    expect(parseAnalysisArtifact(bad, fixture.artifact_id)).toBeNull()
    expect(
      parseAnalysisArtifact({ ...fixture, analysis_result: null }, fixture.artifact_id),
    ).toBeNull()
    expect(
      parseAnalysisArtifact({ ...fixture, oversized: 'a'.repeat(2_000_000) }, fixture.artifact_id),
    ).toBeNull()
  })
  it('refuses numeric strings or impossible prediction bounds', () => {
    const bad = structuredClone(fixture)
    const prediction = bad.analysis_result.blocks.find((b) => b.type === 'prediction')!
    ;(prediction.points![0]! as Record<string, unknown>).lower_bound = 9999
    expect(parseAnalysisArtifact(bad, fixture.artifact_id)).toBeNull()
    const series = structuredClone(fixture)
    const block = series.analysis_result.blocks.find((b) => b.type === 'timeseries')!
    ;(block.points![0]! as Record<string, unknown>).speed = 'not-a-number'
    expect(parseAnalysisArtifact(series, fixture.artifact_id)).toBeNull()
  })
})
