import type { ResultReference } from '@/api/workspace'
import type { AnalysisResult, TrajectoryBlock } from './analysisDisplay'

export interface ReceiptView {
  reference: ResultReference
  data: unknown
  analysis: AnalysisResult | null
}
export interface ReceiptPresentation {
  id: string
  label: string
  analysis: AnalysisResult
  sources: ReceiptView[]
  note: string
}
const object = (v: unknown): Record<string, unknown> =>
  v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0
const names: Record<string, string> = {
  get_scat_track_points: '轨迹点',
  get_scat_flight_overview: '航班概况',
  get_scat_flight_phase_stats: '飞行阶段统计',
  get_scat_fpl_overview: '飞行计划',
  search_scat_flights: '航班检索',
  list_scat_batches: '数据批次',
  get_scat_batch_overview: '批次概况',
  inspect_scat_model_data_quality: '模型输入窗口质量',
  list_scat_quality_problem_flights: '问题航班',
  list_scat_quality_clean_flights: '质量合格航班',
  get_scat_airport_stats: '机场统计',
  get_scat_prediction_error_stats: '已有空管预测误差',
  get_prediction_metric_summaries: 'CFPL批次物化汇总',
  get_worst_prediction_cases: 'CFPL最差样本',
  list_uav_models: 'UAV 机型',
  search_uav_flights: 'UAV 架次检索',
  get_uav_flight_overview: 'UAV 架次概况',
  get_uav_samples: 'UAV 传感器样本',
  get_uav_channel_stats: 'UAV 通道统计',
  get_uav_normal_vs_anomaly_stats: 'UAV 正常与异常对比',
  get_uav_phase_stats: 'UAV 飞行阶段',
}
export const receiptName = (op: string) => names[op] ?? op.replaceAll('_', ' ')
export function receiptRange(source: ReceiptView): string {
  const result = object(source.data),
    query = object(result.query),
    data = object(result.data)
  const page = object(data.pagination)
  const flight = query.external_id == null ? '' : `航班 ${query.external_id} · `
  if (!integer(page.start) || !integer(page.source_points_consumed))
    return flight + receiptName(source.reference.operation)
  const end = page.start + page.source_points_consumed - 1
  const count = Array.isArray(data.points) ? data.points.length : 0
  return `${flight}源序号 ${page.start}–${end} · ${count}点 · ${query.every_nth === 1 ? '逐点' : `每${query.every_nth}点抽1点`}`
}

/** Merge only verified, compatible trajectory geometry, using original source indices.
 * Different flights, time origins, units, fields and conflicting overlap stay separate.
 * No interpolation, prediction, or synthetic observation times are introduced.
 */
export function presentReceipts(sources: ReceiptView[]): ReceiptPresentation[] {
  const groups: ReceiptPresentation[] = []
  const keys = new Map<
    string,
    {
      group: ReceiptPresentation
      points: Map<number, TrajectoryBlock['points'][number]>
      block: TrajectoryBlock
      duplicates: number
      total: number
    }
  >()
  for (const source of sources) {
    const analysis = source.analysis
    if (!analysis) continue
    const block = analysis.blocks.find((b): b is TrajectoryBlock => b.type === 'trajectory_2d')
    const result = object(source.data),
      query = object(result.query),
      page = object(object(result.data).pagination)
    const rawPoints = object(result.data).points
    const compatible =
      block &&
      source.reference.operation === 'get_scat_track_points' &&
      typeof query.batch === 'string' &&
      typeof query.external_id === 'string' &&
      (!block.time_key ||
        (typeof analysis.start_time === 'string' &&
          Number.isFinite(Date.parse(analysis.start_time)))) &&
      integer(page.start) &&
      integer(page.source_points_consumed) &&
      integer(page.total_points) &&
      Number(page.start) + Number(page.source_points_consumed) <= Number(page.total_points) &&
      integer(query.every_nth) &&
      query.every_nth > 0 &&
      Array.isArray(rawPoints) &&
      rawPoints.length === block.points.length &&
      (block.points.length === 0 ||
        (block.points.length - 1) * query.every_nth < page.source_points_consumed)
    if (!compatible || !block) {
      groups.push({
        id: source.reference.result_id,
        label: analysis.title,
        analysis,
        sources: [source],
        note: '单次查询结果；来源明细可单独查看。',
      })
      continue
    }
    const layout = { ...block }
    delete (layout as Partial<TrajectoryBlock>).points
    const scope = { ...query }
    for (const key of ['start', 'limit', 'every_nth', 'fields']) delete scope[key]
    const key = JSON.stringify([
      scope,
      layout,
      page.total_points,
      block.time_key ? analysis.start_time : null,
    ])
    const indices = block.points.map(
      (point, i) => [Number(page.start) + i * Number(query.every_nth), point] as const,
    )
    let target = keys.get(key)
    // A conflict is kept as its own source rather than silently overwriting a value.
    if (
      target &&
      indices.some(
        ([index, point]) =>
          target!.points.has(index) &&
          JSON.stringify(target!.points.get(index)) !== JSON.stringify(point),
      )
    )
      target = undefined
    if (!target) {
      const group: ReceiptPresentation = {
        id: source.reference.result_id,
        label: receiptRange(source),
        analysis: { ...analysis, blocks: [] },
        sources: [],
        note: '',
      }
      target = { group, points: new Map(), block, duplicates: 0, total: Number(page.total_points) }
      if (!keys.has(key)) keys.set(key, target)
      groups.push(group)
    }
    target.group.sources.push(source)
    for (const [index, point] of indices) {
      if (target.points.has(index)) target.duplicates++
      else target.points.set(index, point)
    }
    const ordered = [...target.points].sort(([a], [b]) => a - b)
    const complete = ordered.length === target.total && ordered.every(([index], i) => index === i)
    const context = { ...analysis.context }
    delete context.readonly_result_id
    delete context.readonly_payload_sha256
    target.group.analysis = {
      ...analysis,
      title: `航班 ${query.external_id} · 轨迹展示`,
      context: {
        ...context,
        display_scope: 'loaded_receipts',
        source_receipt_count: target.group.sources.length,
      },
      blocks: [
        {
          type: 'metrics',
          title: '已加载轨迹范围',
          items: [
            { key: 'total', label: '轨迹总点数', value: target.total, unit: '个' },
            { key: 'loaded', label: '已加载去重点数', value: ordered.length, unit: '个' },
            {
              key: 'sources',
              label: '数据来源',
              value: target.group.sources.length,
              unit: '次读取',
            },
          ],
        },
        { ...block, points: ordered.map(([, point]) => point) },
      ],
    }
    target.group.note = `合并${target.group.sources.length}次读取，去重${target.duplicates}点；已加载${ordered.length}/${target.total}点。${complete ? '已覆盖完整源序列。' : '仅展示已加载范围，抽样点按点序连接，不代表完整逐点航迹。'}`
  }
  for (const group of groups)
    if (group.sources.length === 1) group.analysis = group.sources[0]!.analysis!
  return groups.sort(
    (a, b) =>
      Number(b.analysis.blocks.some((c) => c.type === 'trajectory_2d')) -
      Number(a.analysis.blocks.some((c) => c.type === 'trajectory_2d')),
  )
}
