import { describe, expect, it } from 'vitest'

import type { MetricSummaryPage, MetricSummaryRow, WorstPredictionSamplePage } from '@/api/client'
import { buildMetricSummaryCsv, buildWorstSamplesCsv, metricGroupLabel } from './evaluationExport'

function summaryRow(overrides: Partial<MetricSummaryRow> = {}): MetricSummaryRow {
  return {
    batch: 'scat20161015_20161021',
    horizon_s: 300,
    group_by: 'overall',
    group_value: 'all',
    sample_count: 411614,
    flight_count: 10597,
    horizontal_mae_m: 4690.3,
    horizontal_rmse_m: 8123.4,
    vertical_mae_m: 444.4,
    vertical_rmse_m: 601.2,
    along_track_mae_m: 1234.5,
    cross_track_mae_m: 3456.7,
    horizontal_median_m: 3268.6,
    horizontal_p90_m: 11500.0,
    horizontal_p95_m: 13815.6,
    vertical_median_m: 210.0,
    vertical_p90_m: 900.0,
    vertical_p95_m: 1200.0,
    percentile_method: 'approximate',
    metric_version: 'scat-prediction-metrics-v2',
    algorithm_version: '2',
    source_version: 'scat-import-2',
    generated_at: '2026-09-19T19:20:44Z',
    ...overrides,
  }
}

const summaryPage: MetricSummaryPage = {
  batch: 'scat20161015_20161021',
  group_by: null,
  horizon_s: null,
  record_count: 2,
  summaries: [
    summaryRow(),
    summaryRow({
      group_by: 'phase',
      group_value: 'high_cruise',
      horizontal_mae_m: 5120.0,
      vertical_mae_m: null,
      percentile_method: null,
    }),
  ],
}

const samplePage: WorstPredictionSamplePage = {
  batch: 'scat20161015_20161021',
  metric: 'horizontal_error_m',
  horizon_s: 300,
  sample_count: 1,
  samples: [
    {
      batch: 'scat20161015_20161021',
      external_id: '108567',
      snapshot_time: '2016-10-20T06:37:02.553000Z',
      horizon_s: 300,
      horizontal_error_m: 176906.4,
      vertical_error_m: -7155.0,
      along_track_error_m: 842.2,
      cross_track_error_m: -9764.8,
      is_valid: true,
      dominant_phase: 'high_cruise',
      aircraft_type: 'A388',
    },
  ],
}

describe('evaluationExport', () => {
  it('maps group keys to readable labels', () => {
    expect(metricGroupLabel(summaryRow())).toBe('整批次：all')
    expect(metricGroupLabel(summaryRow({ group_by: 'phase', group_value: 'climb' }))).toBe(
      '主导阶段：climb',
    )
    expect(metricGroupLabel(summaryRow({ group_by: 'custom', group_value: 'x' }))).toBe('custom：x')
  })

  it('builds a metric summary csv with version, samples and empty cells for nulls', () => {
    const csv = buildMetricSummaryCsv(summaryPage)

    expect(csv.startsWith('\uFEFF')).toBe(true)
    expect(csv).toContain('SCAT 预测评估 · 指标汇总,scat20161015_20161021')
    expect(csv).toContain('指标版本,scat-prediction-metrics-v2')
    expect(csv).toContain('整批次：all,300,411614,10597,4690.3,8123.4')
    expect(csv).toContain('主导阶段：high_cruise,300,411614,10597,5120')
    // 垂直 MAE 缺失时留空，不写 0
    expect(csv).toContain('5120,8123.4,3268.6,11500,13815.6,,601.2,210,900,1200,1234.5,3456.7')
    expect(csv.split('\r\n')).toHaveLength(7)
  })

  it('builds a worst-sample csv with the ranking field and horizon', () => {
    const csv = buildWorstSamplesCsv(samplePage)

    expect(csv).toContain('SCAT 预测评估 · 最差案例,scat20161015_20161021')
    expect(csv).toContain('排序字段,horizontal_error_m')
    expect(csv).toContain('时域(s),300')
    expect(csv).toContain(
      '航班,快照时刻(UTC),时域(s),水平误差(m),垂直误差(m),沿航向(m),横向(m),主导阶段,机型',
    )
    expect(csv).toContain(
      '108567,2016-10-20T06:37:02.553000Z,300,176906.4,-7155,842.2,-9764.8,high_cruise,A388',
    )
  })

  it('quotes cells containing commas or quotes', () => {
    const csv = buildWorstSamplesCsv({
      ...samplePage,
      samples: [{ ...samplePage.samples[0], dominant_phase: 'climb, "transition"' }],
    })

    expect(csv).toContain('"climb, ""transition"""')
  })
})
