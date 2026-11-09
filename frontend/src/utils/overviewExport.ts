import type { BatchOverview, BatchSummary } from '@/api/client'

function csvCell(value: string | number) {
  const text = String(value)
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
}

export function buildOverviewCsv(summary: BatchSummary, overview: BatchOverview) {
  const rows: Array<Array<string | number>> = [
    ['SCAT 批次总览', summary.batch],
    ['计划开始时间（UTC）', summary.earliest_iobt_utc ?? ''],
    ['计划结束时间（UTC）', summary.latest_iobt_utc ?? ''],
    [],
    ['指标', '数值'],
    ['航班数', overview.statistics.flight_count],
    ['轨迹点', overview.statistics.total_track_points],
    ['含预测航班', overview.statistics.flights_with_prediction],
    ['预测快照', overview.statistics.total_prediction_snapshots],
    [],
    ['机型', '航班数'],
    ...overview.top_aircraft_types.map((item) => [item.aircraft_type, item.flight_count]),
    [],
    ['航线', '航班数'],
    ...overview.top_routes.map((item) => [`${item.adep}-${item.ades}`, item.flight_count]),
    [],
    ['起飞机场', '航班数'],
    ...overview.top_departure_airports.map((item) => [item.airport, item.flight_count]),
    [],
    ['到达机场', '航班数'],
    ...overview.top_arrival_airports.map((item) => [item.airport, item.flight_count]),
    [],
    ['时间（UTC）', '航班数'],
    ...overview.flight_volume.map((item) => [item.period_start, item.flight_count]),
  ]

  return `\uFEFF${rows.map((row) => row.map(csvCell).join(',')).join('\r\n')}`
}

export function downloadOverviewCsv(summary: BatchSummary, overview: BatchOverview) {
  const blob = new Blob([buildOverviewCsv(summary, overview)], {
    type: 'text/csv;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${summary.batch}-overview.csv`
  anchor.click()
  URL.revokeObjectURL(url)
}
