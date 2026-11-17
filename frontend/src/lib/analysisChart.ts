import type { EChartsCoreOption } from 'echarts/core'
import { withUnit, type AnalysisBlock, type AnalysisResult } from './analysisDisplay'

type Value = string | number | boolean | null | undefined
type Field = { key: string; label: string; unit?: string | null }
type AxisField = Field & { kind: 'number' | 'time' | 'category' }
export type ChartInput = {
  fields: AxisField[]
  rows: Record<string, Value>[]
  preferredX?: string
}
export type ChartKind = 'bar' | 'line' | 'scatter' | 'heatmap' | 'boxplot'
export type ChartView = { kind: ChartKind; x: string; y: string; group?: string }

const boxKeys = ['box_min', 'box_q1', 'box_median', 'box_q3', 'box_max'] as const
const distributionKeys = new Set(['min', 'q1', 'median', 'q3', 'max'])

function boxRows(input: ChartInput, x: string) {
  const fields = boxKeys.map((key) =>
    input.fields.find((field) => field.key === key && field.kind === 'number'),
  )
  if (fields.some((field) => !field || field.unit !== fields[0]?.unit)) return []
  return input.rows.filter(
    (row) =>
      row[x] != null &&
      boxKeys.every((key, index) => {
        const value = row[key]
        return finite(value) && (!index || Number(row[boxKeys[index - 1]!]) <= value)
      }),
  )
}

const finite = (value: Value): value is number =>
  typeof value === 'number' && Number.isFinite(value)
const isDate = (value: Value): value is string =>
  typeof value === 'string' &&
  /^\d{4}-\d{2}-\d{2}(?:T.+(?:Z|[+-]\d{2}:\d{2}))?$/.test(value) &&
  Number.isFinite(Date.parse(value))

/** Infer only the field type. The selected columns and source rows remain visible to the user. */
export function chartInput(block: AnalysisBlock): ChartInput | null {
  if (block.type === 'timeseries') {
    return {
      fields: [
        {
          key: block.x_key,
          label: block.x_label,
          unit: block.x_unit,
          kind: block.x_type === 'datetime' ? 'time' : 'number',
        },
        ...block.series
          .filter((field) => block.points.some((row) => finite(row[field.key])))
          .map((field) => ({ ...field, kind: 'number' as const })),
      ],
      rows: block.points,
      preferredX: block.x_key,
    }
  }
  if (block.type !== 'table') return null
  const units = new Set(block.rows.map((row) => row.unit).filter((unit) => unit != null))
  const fields = block.columns.flatMap<AxisField>((field) => {
    if (units.size > 1 && distributionKeys.has(field.key)) return []
    const values = block.rows.map((row) => row[field.key]).filter((value) => value != null)
    if (!values.length) return []
    if (values.every(finite)) return [{ ...field, kind: 'number' }]
    if (values.every(isDate)) return [{ ...field, kind: 'time' }]
    if (values.every((value) => typeof value === 'string' || typeof value === 'boolean'))
      return [{ ...field, kind: 'category' }]
    return []
  })
  return { fields, rows: block.rows }
}

export function resultChartInput(
  result: AnalysisResult,
  index: number,
  block = result.blocks[index]!,
) {
  const input = chartInput(block)
  if (
    !input ||
    block.type !== 'timeseries' ||
    result.operation !== 'diagnose_uav_channels' ||
    result.blocks.findIndex((item) => item.type === 'timeseries') !== index
  )
    return input
  const rows = new Map(input.rows.map((row) => [row[block.x_key], { ...row }]))
  const fields = [...input.fields]
  for (const candidate of result.blocks.slice(index + 1)) {
    if (
      candidate.type !== 'timeseries' ||
      candidate.x_key !== block.x_key ||
      candidate.x_type !== block.x_type
    )
      continue
    const other = chartInput(candidate)!
    for (const field of other.fields) {
      if (fields.some((value) => value.key === field.key)) continue
      fields.push(field)
      for (const value of other.rows) {
        const matching = rows.get(value[block.x_key])
        if (matching) matching[field.key] = value[field.key]
      }
    }
  }
  return { ...input, fields, rows: [...rows.values()] }
}

function paired(input: ChartInput, x: string, y: string, group?: string) {
  return input.rows.filter(
    (row) => row[x] != null && row[y] != null && (!group || row[group] != null),
  )
}

export function chartAxes(input: ChartInput, kind: ChartKind, x?: string): AxisField[] {
  if (x !== undefined)
    return input.fields.filter(
      (field) =>
        field.key !== x && field.kind === 'number' && paired(input, x, field.key).length > 0,
    )
  return input.fields.filter((field) => {
    if (kind === 'boxplot') return field.kind === 'category' && boxRows(input, field.key).length > 0
    if ((kind === 'line' || kind === 'scatter') && field.kind === 'category') return false
    if (kind === 'heatmap') return heatmapGroups(input, field.key).length > 0
    return chartAxes(input, kind, field.key).length > 0
  })
}

function heatmapGroups(input: ChartInput, x: string, y?: string) {
  const metrics = input.fields.filter(
    (field) => field.kind === 'number' && field.key !== x && (!y || field.key === y),
  )
  return input.fields.filter(
    (field) =>
      field.kind === 'category' &&
      field.key !== x &&
      metrics.some((metric) => {
        const rows = paired(input, x, metric.key, field.key)
        return (
          rows.length > 0 &&
          new Set(rows.map((row) => JSON.stringify([String(row[x]), String(row[field.key])])))
            .size === rows.length
        )
      }),
  )
}

export function chartMetrics(input: ChartInput, kind: ChartKind, x: string) {
  return chartAxes(input, kind, x).filter(
    (field) =>
      (kind !== 'heatmap' || heatmapGroups(input, x, field.key).length > 0) &&
      (kind !== 'boxplot' || field.key === 'box_median'),
  )
}

export function chartGroups(input: ChartInput, view: ChartView) {
  if (view.kind === 'heatmap') return heatmapGroups(input, view.x, view.y)
  if (view.kind === 'boxplot') return []
  return input.fields.filter((field) => {
    if (field.kind !== 'category' || field.key === view.x) return false
    const size = new Set(paired(input, view.x, view.y, field.key).map((row) => row[field.key])).size
    return size >= 2 && size <= 8
  })
}

export function defaultChart(
  input: ChartInput,
  kind: ChartKind,
  x?: string,
): ChartView | undefined {
  const xs = chartAxes(input, kind)
  const key =
    xs.find((field) => field.key === x)?.key ??
    xs.find((field) => field.key === input.preferredX)?.key ??
    xs.find((field) => field.key === 'horizon_s' || field.key === 'bin')?.key ??
    xs.find((field) => field.kind !== 'number')?.key ??
    xs[0]?.key
  if (!key) return undefined
  const metrics = chartMetrics(input, kind, key)
  const y =
    metrics.find(
      (field) =>
        (kind === 'heatmap' && field.key === 'pearson_r') ||
        (key === 'bin' && field.key === 'count'),
    )?.key ?? metrics[0]?.key
  if (!y) return undefined
  const view: ChartView = { kind, x: key, y }
  return kind === 'heatmap' ? { ...view, group: chartGroups(input, view)[0]!.key } : view
}

export function validChart(input: ChartInput | null, value: unknown): value is ChartView {
  if (!input || typeof value !== 'object' || value === null) return false
  const view = value as ChartView
  return (
    (['line', 'bar', 'scatter', 'heatmap', 'boxplot'] as const).includes(view.kind) &&
    chartAxes(input, view.kind).some((field) => field.key === view.x) &&
    chartMetrics(input, view.kind, view.x).some((field) => field.key === view.y) &&
    (view.group === undefined
      ? view.kind !== 'heatmap'
      : chartGroups(input, view).some((field) => field.key === view.group))
  )
}

export function chartRows(input: ChartInput, view: ChartView) {
  if (view.kind === 'boxplot') return boxRows(input, view.x)
  // Keep missing Y values as gaps on time/numerical lines rather than connecting across them.
  return view.kind === 'line'
    ? input.rows.filter((row) => row[view.x] != null && (!view.group || row[view.group] != null))
    : paired(input, view.x, view.y, view.group)
}

export function chartColumns(input: ChartInput, view: ChartView) {
  const keys = view.kind === 'boxplot' ? [view.x, ...boxKeys] : [view.x, view.y, view.group]
  return keys.flatMap((key) => input.fields.filter((field) => field.key === key))
}

export function resultChart(input: ChartInput, view: ChartView): EChartsCoreOption {
  const x = input.fields.find((field) => field.key === view.x)!
  const y = input.fields.find((field) => field.key === view.y)!
  const group = input.fields.find((field) => field.key === view.group)
  const rows = chartRows(input, view)
  const common = {
    animation: false,
    useUTC: true,
    tooltip: { trigger: 'item', renderMode: 'richText' },
    grid: { left: '14%', right: '8%', top: 30, bottom: 85 },
  }
  const name = (field: Field) => withUnit(field.label, field.unit)
  if (view.kind === 'boxplot') {
    return {
      ...common,
      xAxis: {
        type: 'category',
        name: name(x),
        data: rows.map((row) => row[x.key]),
        axisLabel: { hideOverlap: true },
      },
      yAxis: { type: 'value', scale: true, name: y.unit ? `数值（${y.unit}）` : '数值' },
      series: [
        {
          type: 'boxplot',
          name: '最小值、P25、P50、P75、最大值',
          data: rows.map((row) => boxKeys.map((key) => row[key])),
        },
      ],
    }
  }
  if (view.kind === 'heatmap' && group) {
    const xs = [...new Set(rows.map((row) => String(row[x.key])))],
      ys = [...new Set(rows.map((row) => String(row[group.key])))]
    const values = rows.map((row) => Number(row[y.key]))
    return {
      ...common,
      xAxis: { type: 'category', name: name(x), data: xs },
      yAxis: { type: 'category', name: name(group), data: ys },
      visualMap: {
        min: y.key === 'pearson_r' ? -1 : Math.min(...values),
        max: y.key === 'pearson_r' ? 1 : Math.max(...values),
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
      },
      series: [
        {
          type: 'heatmap',
          name: name(y),
          data: rows.map((row) => [
            xs.indexOf(String(row[x.key])),
            ys.indexOf(String(row[group.key])),
            row[y.key],
          ]),
        },
      ],
    }
  }
  const categories = x.kind === 'category' || view.kind === 'bar'
  const sortValue = (value: Value) =>
    x.kind === 'time' ? Date.parse(String(value)) : Number(value)
  const ordered = categories
    ? rows
    : [...rows].sort((a, b) => sortValue(a[x.key]) - sortValue(b[x.key]))
  const groups = group ? [...new Set(ordered.map((row) => String(row[group.key])))] : [name(y)]
  const labels = categories ? ordered.map((row) => String(row[x.key])) : []
  const byCategory = new Map<string, Map<string, Value>>()
  if (categories && group) {
    for (const row of ordered) {
      const label = String(row[x.key])
      const series = String(row[group.key])
      const values = byCategory.get(label) ?? new Map<string, Value>()
      if (values.has(series)) {
        byCategory.clear()
        break
      }
      values.set(series, row[y.key])
      byCategory.set(label, values)
    }
  }
  return {
    ...common,
    legend: { type: 'scroll', bottom: 28 },
    dataZoom:
      ordered.length > 12 ? [{ type: 'inside' }, { type: 'slider', bottom: 0, height: 20 }] : [],
    xAxis: {
      type: categories ? 'category' : x.kind === 'time' ? 'time' : 'value',
      name: name(x),
      nameLocation: 'middle',
      nameGap: 30,
      ...(categories ? { data: byCategory.size ? [...byCategory.keys()] : labels } : {}),
      axisLabel: { hideOverlap: true },
    },
    yAxis: { type: 'value', name: name(y), scale: view.kind !== 'bar' },
    series: groups.map((label) => ({
      name: label,
      type: view.kind,
      connectNulls: false,
      ...(view.kind === 'line' && y.key === 'cdf_percent' ? { step: 'end' } : {}),
      symbolSize: 6,
      data: categories
        ? byCategory.size
          ? [...byCategory.values()].map((values) => values.get(label) ?? null)
          : ordered.map((row) => (!group || String(row[group.key]) === label ? row[y.key] : null))
        : ordered
            .filter((row) => !group || String(row[group.key]) === label)
            .map((row) => [sortValue(row[x.key]), row[y.key] ?? null]),
    })),
  }
}

export function resultCsv(fields: Field[], rows: Record<string, Value>[]) {
  const cells = (values: Value[]) =>
    values
      .map((value) => {
        const text = String(value ?? '')
        const safe =
          typeof value === 'string' && /^\s*[=+@-]|^[\t\r\n]/.test(text) ? `'${text}` : text
        return `"${safe.replaceAll('"', '""')}"`
      })
      .join(',')
  return (
    '\uFEFF' +
    [
      cells(fields.map((field) => withUnit(field.label, field.unit))),
      ...rows.map((row) => cells(fields.map((field) => row[field.key]))),
    ].join('\r\n')
  )
}
