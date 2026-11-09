import type { EChartsCoreOption } from 'echarts/core'
import type { components } from '@/api/schema'

type RawResult = components['schemas']['AnalysisResult']
type WithDefaults<T> = T extends object
  ? T & Required<Pick<T, Extract<keyof T, 'type' | 'rows' | 'points' | 'series' | 'items'>>>
  : never
export type AnalysisBlock = WithDefaults<RawResult['blocks'][number]>
export type AnalysisResult = Omit<RawResult, 'blocks' | 'schema_version'> & {
  schema_version: NonNullable<RawResult['schema_version']>
  blocks: AnalysisBlock[]
}
export type TrajectoryBlock = Extract<AnalysisBlock, { type?: 'trajectory_2d' }>
type Scalar = string | number | boolean | null | undefined

const record = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.length <= 2000
const finite = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)
const scalar = (value: unknown) =>
  value === null || typeof value === 'boolean' || text(value) || finite(value)
const list = (value: unknown, maximum: number, check: (item: unknown) => boolean) =>
  Array.isArray(value) && value.length <= maximum && value.every(check)
const rows = (value: unknown, maximum: number) =>
  list(value, maximum, (item) => record(item) && Object.values(item).every(scalar))
const fields = (value: unknown) =>
  record(value) && text(value.key) && text(value.label) && (value.unit == null || text(value.unit))

function validBlock(block: unknown): boolean {
  if (!record(block)) return false
  if (
    Object.entries(block).some(
      ([key, value]) => key.endsWith('_unit') && value != null && !text(value),
    )
  )
    return false
  if (block.type === 'action_link') return text(block.label) && text(block.url)
  if (!text(block.title)) return false
  switch (block.type) {
    case 'metrics':
      return list(
        block.items,
        12,
        (item) => fields(item) && scalar((item as Record<string, unknown>).value),
      )
    case 'table':
      return list(block.columns, 30, fields) && rows(block.rows, 100)
    case 'timeseries': {
      if (!(
        text(block.x_key) &&
        text(block.x_label) &&
        ['number', 'datetime'].includes(String(block.x_type)) &&
        list(block.series, 8, fields) &&
        rows(block.points, 200)
      ))
        return false
      const series = block.series as { key: string }[]
      return (block.points as Record<string, unknown>[]).every(
        (point) =>
          (block.x_type === 'number'
            ? finite(point[block.x_key as string])
            : typeof point[block.x_key as string] === 'string' &&
              Number.isFinite(Date.parse(String(point[block.x_key as string])))) &&
          series.every((item) => point[item.key] == null || finite(point[item.key])),
      )
    }
    case 'trajectory_2d':
      return (
        text(block.x_key) &&
        text(block.y_key) &&
        text(block.x_label) &&
        text(block.y_label) &&
        list(block.series, 3, fields) &&
        rows(block.points, 5000) &&
        (block.points as Record<string, unknown>[]).every((point) =>
          [block.x_key, block.y_key].every(
            (key) => point[key as string] == null || finite(point[key as string]),
          ),
        )
      )
    case 'classification':
      return (
        list(block.class_labels, 20, text) &&
        list(
          block.items,
          200,
          (item) =>
            record(item) &&
            text(item.label) &&
            (block.class_labels as string[]).includes(item.label) &&
            Number.isInteger(item.count ?? 1) &&
            Number(item.count ?? 1) >= 0 &&
            (item.confidence == null ||
              (finite(item.confidence) && item.confidence >= 0 && item.confidence <= 1)),
        )
      )
    case 'prediction':
      return (
        text(block.source) &&
        text(block.target_label) &&
        list(
          block.points,
          200,
          (point) =>
            record(point) &&
            finite(point.time_step_s) &&
            finite(point.value) &&
            (point.lower_bound == null || finite(point.lower_bound)) &&
            (point.upper_bound == null || finite(point.upper_bound)) &&
            (point.lower_bound == null || Number(point.lower_bound) <= point.value) &&
            (point.upper_bound == null || Number(point.upper_bound) >= point.value),
        )
      )
    default:
      return false
  }
}

/** The backend validates the full protocol; this is a bounded display boundary for fetched artifacts. */
export function parseAnalysisArtifact(payload: unknown, artifactId: string): AnalysisResult | null {
  try {
    if (
      !record(payload) ||
      payload.artifact_id !== artifactId ||
      JSON.stringify(payload).length > 2_000_000
    )
      return null
    const result = payload.analysis_result
    if (
      !record(result) ||
      !/^1\.[0-6]$/.test(String(result.schema_version)) ||
      !['scat', 'uav', 'cross_dataset'].includes(String(result.dataset)) ||
      !text(result.title) ||
      !text(result.operation) ||
      !record(result.context) ||
      !Array.isArray(result.blocks) ||
      result.blocks.length === 0 ||
      !list(result.blocks, 20, validBlock)
    )
      return null
    return result as AnalysisResult
  } catch {
    return null
  }
}

export const scalarLabel = (value: Scalar): string =>
  value == null ? '未提供' : typeof value === 'boolean' ? (value ? '是' : '否') : String(value)
export const withUnit = (label: string, unit?: string | null) =>
  unit ? `${label}（${unit}）` : label

/** Never navigate protocol-relative, script, credential-bearing, or secret-bearing URLs. */
export function safeAnalysisLink(value: string, origin: string): string | null {
  const unsafeCharacters = (input: string, spaces: boolean) =>
    [...input].some(
      (character) =>
        character === '\\' ||
        character.charCodeAt(0) < (spaces ? 33 : 32) ||
        character.charCodeAt(0) === 127,
    )
  if (unsafeCharacters(value, true) || value.startsWith('//')) return null
  try {
    const decoded = decodeURIComponent(value)
    if (decoded.startsWith('//') || unsafeCharacters(decoded, false)) return null
    const url = new URL(value, origin)
    if (!value.startsWith('/') && !/^https?:\/\//i.test(value)) return null
    if (url.username || url.password || !['http:', 'https:'].includes(url.protocol)) return null
    if (url.protocol === 'http:' && url.origin !== origin) return null
    if (
      [...url.searchParams.keys()].some((key) => /token|secret|api.?key|authorization/i.test(key))
    )
      return null
    return url.origin === origin ? url.pathname + url.search + url.hash : url.href
  } catch {
    return null
  }
}

const common = {
  animation: false,
  tooltip: { trigger: 'axis' as const, renderMode: 'richText' as const },
  legend: { type: 'scroll' as const, bottom: 0 },
  grid: { top: 40, left: 55, right: 20, bottom: 70 },
}

export function blockChart(block: AnalysisBlock): EChartsCoreOption | null {
  if (block.type === 'timeseries') {
    return {
      ...common,
      xAxis: {
        type: block.x_type === 'datetime' ? 'time' : 'value',
        name: withUnit(block.x_label, block.x_unit),
        nameLocation: 'middle',
        nameGap: 30,
      },
      yAxis: { type: 'value', name: '数值（单位见图例）' },
      series: block.series.map((series) => ({
        name: withUnit(series.label, series.unit),
        type: 'line',
        connectNulls: false,
        showSymbol: true,
        symbolSize: 6,
        data: block.points.map((point) => [point[block.x_key], point[series.key] ?? null]),
      })),
    }
  }
  if (block.type === 'classification') {
    return {
      ...common,
      xAxis: { type: 'category', data: block.class_labels },
      yAxis: { type: 'value', name: '样本数', minInterval: 1 },
      series: [
        {
          name: '样本数',
          type: 'bar',
          data: block.class_labels.map((label) =>
            block.items
              .filter((item) => item.label === label)
              .reduce((sum, item) => sum + (item.count ?? 1), 0),
          ),
        },
      ],
    }
  }
  if (block.type === 'prediction') {
    const series = [
      {
        name: block.target_label,
        type: 'line',
        data: block.points.map((point) => [point.time_step_s, point.value]),
      },
    ]
    for (const key of ['lower_bound', 'upper_bound'] as const) {
      if (block.points.some((point) => point[key] != null))
        series.push({
          name: key === 'lower_bound' ? '下界' : '上界',
          type: 'line',
          data: block.points.map((point) => [point.time_step_s, point[key] ?? null]) as number[][],
        })
    }
    return {
      ...common,
      xAxis: { type: 'value', name: '预测时距（s）', nameLocation: 'middle', nameGap: 30 },
      yAxis: { type: 'value', scale: true, name: withUnit(block.target_label, block.target_unit) },
      series,
    }
  }
  return null
}

export function trajectoryChart(block: TrajectoryBlock, snapshot: string): EChartsCoreOption {
  const groups = new Map<string, typeof block.points>()
  for (const point of block.points) {
    if (block.snapshot_key && scalarLabel(point[block.snapshot_key]) !== snapshot) continue
    const label =
      [block.path_key, block.segment_key]
        .filter(Boolean)
        .map((key) => scalarLabel(point[key!]))
        .join(' · ') || '轨迹'
    const group = groups.get(label) ?? []
    group.push(point)
    groups.set(label, group)
  }
  return {
    ...common,
    xAxis: {
      type: 'value',
      scale: true,
      name: withUnit(block.x_label, block.x_unit),
      nameLocation: 'middle',
      nameGap: 30,
    },
    yAxis: { type: 'value', scale: true, name: withUnit(block.y_label, block.y_unit) },
    series: [...groups].map(([name, points]) => ({
      name,
      type: 'line',
      showSymbol: true,
      symbolSize: 5,
      connectNulls: false,
      data: points.map((point) =>
        finite(point[block.x_key]) && finite(point[block.y_key])
          ? [point[block.x_key], point[block.y_key]]
          : [null, null],
      ),
    })),
  }
}
