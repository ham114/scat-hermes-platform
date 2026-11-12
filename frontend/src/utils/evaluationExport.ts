import type { MetricSummaryPage, MetricSummaryRow, WorstPredictionSamplePage } from '@/api/client'

function csvCell(value: string | number) {
  const text = String(value)
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
}

const GROUP_LABELS: Record<string, string> = {
  overall: '整批次',
  phase: '主导阶段',
  aircraft_type: '机型',
}

/** 汇总行的分组标签：`阶段：high_cruise` 这类便于导出的可读写法。 */
export function metricGroupLabel(row: MetricSummaryRow) {
  const group = GROUP_LABELS[row.group_by] ?? row.group_by
  return `${group}：${row.group_value}`
}

function metricVersion(page: MetricSummaryPage) {
  return page.summaries[0]?.metric_version ?? ''
}

export function buildMetricSummaryCsv(page: MetricSummaryPage) {
  const rows: Array<Array<string | number>> = [
    ['SCAT 预测评估 · 指标汇总', page.batch],
    ['指标版本', metricVersion(page)],
    ['聚合口径', '按快照（每快照每时域 1 条）；分组汇总均带样本量'],
    [],
    [
      '分组',
      '时域(s)',
      '样本量',
      '航班数',
      '水平MAE(m)',
      '水平RMSE(m)',
      '水平中位数(m)',
      '水平P90(m)',
      '水平P95(m)',
      '垂直MAE(m)',
      '垂直RMSE(m)',
      '垂直中位数(m)',
      '垂直P90(m)',
      '垂直P95(m)',
      '沿航向MAE(m)',
      '横向MAE(m)',
    ],
    ...page.summaries.map((row) => [
      metricGroupLabel(row),
      row.horizon_s,
      row.sample_count,
      row.flight_count,
      row.horizontal_mae_m ?? '',
      row.horizontal_rmse_m ?? '',
      row.horizontal_median_m ?? '',
      row.horizontal_p90_m ?? '',
      row.horizontal_p95_m ?? '',
      row.vertical_mae_m ?? '',
      row.vertical_rmse_m ?? '',
      row.vertical_median_m ?? '',
      row.vertical_p90_m ?? '',
      row.vertical_p95_m ?? '',
      row.along_track_mae_m ?? '',
      row.cross_track_mae_m ?? '',
    ]),
  ]

  return `\uFEFF${rows.map((row) => row.map(csvCell).join(',')).join('\r\n')}`
}

export function buildWorstSamplesCsv(page: WorstPredictionSamplePage) {
  const rows: Array<Array<string | number>> = [
    ['SCAT 预测评估 · 最差案例', page.batch],
    ['排序字段', page.metric],
    ['时域(s)', page.horizon_s ?? '全部'],
    [],
    [
      '航班',
      '快照时刻(UTC)',
      '时域(s)',
      '水平误差(m)',
      '垂直误差(m)',
      '沿航向(m)',
      '横向(m)',
      '主导阶段',
      '机型',
    ],
    ...page.samples.map((sample) => [
      sample.external_id,
      sample.snapshot_time,
      sample.horizon_s,
      sample.horizontal_error_m ?? '',
      sample.vertical_error_m ?? '',
      sample.along_track_error_m ?? '',
      sample.cross_track_error_m ?? '',
      sample.dominant_phase ?? '',
      sample.aircraft_type ?? '',
    ]),
  ]

  return `\uFEFF${rows.map((row) => row.map(csvCell).join(',')).join('\r\n')}`
}

export function downloadCsv(content: string, fileName: string) {
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  URL.revokeObjectURL(url)
}

export function downloadMetricSummaryCsv(page: MetricSummaryPage) {
  downloadCsv(buildMetricSummaryCsv(page), `${page.batch}-metric-summaries.csv`)
}

export function downloadWorstSamplesCsv(page: WorstPredictionSamplePage) {
  downloadCsv(buildWorstSamplesCsv(page), `${page.batch}-worst-samples.csv`)
}
