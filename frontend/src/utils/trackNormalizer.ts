/** artifact 的 trajectory_2d 块 → 统一渲染数据（纯函数，见 docs/3D_VISUALIZATION.md 第 5 节）。 */

import type { components } from '@/api/schema'
import {
  COMPARISON_TRACK_STYLES,
  DEFAULT_TRAJECTORY_SNAPSHOT_ID,
  SEGMENT_COLORS,
  TRAJECTORY_DOWNSAMPLING_ALGORITHM,
  TRAJECTORY_RENDER_SCHEMA_VERSION,
  type Track3D,
  type TrackPoint3D,
  type TrackSegmentKind,
  type TrajectoryRenderData,
} from '@/visualization/types'

type TrajectoryBlock = components['schemas']['Trajectory2dBlock']
type AnalysisScalar = components['schemas']['AnalysisScalar']

export const DEFAULT_MAX_RENDER_POINTS_PER_TRACK = 2_000
export const MAX_RENDER_POINTS_PER_TRACK = 10_000

export interface TrajectoryRenderOptions {
  maxPointsPerTrack?: number
  includeUntimedGeometry?: boolean
}

export interface DownsampleResult {
  points: TrackPoint3D[]
  semanticAnchorOverflow: boolean
}

interface ErrorCandidate {
  start: number
  end: number
  index: number
  errorSquared: number
}

const PREDICTED_TRACK_PALETTE = ['#f59e0b', '#8b5cf6', '#06b6d4', '#f43f5e', '#84cc16']

function stableStringHash(value: string): number {
  let hash = 0
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0
  }
  return hash
}

function trackPresentation(
  pathValue: string,
  fallbackLabel: string,
  kind: TrackSegmentKind,
): { label: string; color: string } {
  const comparisonStyle = COMPARISON_TRACK_STYLES[pathValue]
  if (comparisonStyle) return comparisonStyle
  return {
    label: pathValue.startsWith('block-') ? fallbackLabel : pathValue,
    color:
      kind === 'predicted'
        ? PREDICTED_TRACK_PALETTE[stableStringHash(pathValue) % PREDICTED_TRACK_PALETTE.length]!
        : SEGMENT_COLORS[kind],
  }
}

function toNumber(value: AnalysisScalar | undefined): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    if (!value.trim()) return null
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return null
}

function segmentKind(value: string | null): TrackSegmentKind {
  if (value === null || value === '') return 'unknown'
  const normalized = value.toLowerCase()
  if (normalized.includes('observed') || normalized.includes('观测')) return 'observed'
  if (normalized.includes('predict') || normalized.includes('预测')) return 'predicted'
  if (
    normalized.includes('actual') ||
    normalized.includes('future') ||
    normalized.includes('真实')
  ) {
    return 'actual_future'
  }
  return 'unknown'
}

function normalizedUnit(value: string | null | undefined): string {
  return (value ?? '').toLowerCase().replaceAll(' ', '')
}

function looksLikeSpeedSeries(key: string, label: string, unit: string): boolean {
  const name = `${key} ${label}`.toLowerCase()
  return (
    name.includes('speed') ||
    name.includes('速度') ||
    name.includes('地速') ||
    ['m/s', 'mps', 'km/h', 'kmh', 'kt', 'kts', 'knot', 'knots'].includes(unit)
  )
}

function normalizedSeriesName(key: string, label: string): string {
  return `${key} ${label}`.toLowerCase().replaceAll('-', '_')
}

/** 在 series 中找高度序列；避免把 m/s 速度误识别为米制高度。 */
function findAltitudeKey(block: TrajectoryBlock): string | null {
  for (const series of block.series ?? []) {
    const name = `${series.key} ${series.label}`.toLowerCase()
    if (name.includes('altitude') || name.includes('height') || name.includes('高度'))
      return series.key
  }
  for (const series of block.series ?? []) {
    const unit = normalizedUnit(series.unit)
    if (unit === 'm' || unit === 'metre' || unit === 'meter') return series.key
  }
  return null
}

/** 在 series 中找展示用地速序列。当前后端标准单位为 m/s。 */
function findGroundSpeedKey(block: TrajectoryBlock): string | null {
  for (const series of block.series ?? []) {
    const name = normalizedSeriesName(series.key, series.label)
    if (
      name.includes('ground_speed') ||
      name.includes('ground speed') ||
      name.includes('horizontal_speed') ||
      name.includes('水平地速') ||
      name.includes('地速')
    ) {
      return series.key
    }
  }
  for (const series of block.series ?? []) {
    const name = normalizedSeriesName(series.key, series.label)
    const isComponent =
      /(^|\s)(vx|vy|rocd)(\s|$)/.test(name) ||
      name.includes('east_velocity') ||
      name.includes('north_velocity') ||
      name.includes('vertical_rate')
    if (
      !isComponent &&
      looksLikeSpeedSeries(series.key, series.label, normalizedUnit(series.unit))
    ) {
      return series.key
    }
  }
  return null
}

function findVelocityComponentKey(
  block: TrajectoryBlock,
  component: 'east' | 'north' | 'vertical',
): string | null {
  for (const series of block.series ?? []) {
    const name = normalizedSeriesName(series.key, series.label)
    if (
      (component === 'east' &&
        (/(^|\s)vx(\s|$)/.test(name) ||
          name.includes('east_velocity') ||
          name.includes('东向速度'))) ||
      (component === 'north' &&
        (/(^|\s)vy(\s|$)/.test(name) ||
          name.includes('north_velocity') ||
          name.includes('北向速度'))) ||
      (component === 'vertical' &&
        (/(^|\s)rocd(\s|$)/.test(name) ||
          name.includes('vertical_rate') ||
          name.includes('垂直速度') ||
          name.includes('升降率')))
    ) {
      return series.key
    }
  }
  return null
}

/** 在 series 中找后端按评估口径产出的逐点水平误差。 */
function findHorizontalErrorKey(block: TrajectoryBlock): string | null {
  for (const series of block.series ?? []) {
    const name = `${series.key} ${series.label}`.toLowerCase()
    if (
      name.includes('horizontal_error') ||
      name.includes('horizontal error') ||
      name.includes('水平误差')
    ) {
      return series.key
    }
  }
  return null
}

function renderPointBudget(options: TrajectoryRenderOptions): number {
  const value = options.maxPointsPerTrack ?? DEFAULT_MAX_RENDER_POINTS_PER_TRACK
  if (!Number.isInteger(value) || value < 2 || value > MAX_RENDER_POINTS_PER_TRACK) {
    throw new RangeError(
      `maxPointsPerTrack must be an integer between 2 and ${MAX_RENDER_POINTS_PER_TRACK}`,
    )
  }
  return value
}

function wrapLongitude(value: number): number {
  return ((((value + 180) % 360) + 360) % 360) - 180
}

function interpolateLongitude(start: number, end: number, ratio: number): number {
  const delta = ((end - start + 540) % 360) - 180
  return wrapLongitude(start + delta * ratio)
}

function longitudeDelta(left: number, right: number): number {
  return ((left - right + 540) % 360) - 180
}

/**
 * 点相对区间端点按时间线性插值的三维误差（平方米）。经纬度使用局部等距近似，
 * 仅用于轨迹内关键点排序；不参与业务 ADE/FDE 计算。
 */
function interpolationErrorSquared(
  point: TrackPoint3D,
  start: TrackPoint3D,
  end: TrackPoint3D,
  indexRatio: number,
): number {
  const timeSpan = end.time - start.time
  const ratio = timeSpan > 0 ? (point.time - start.time) / timeSpan : indexRatio
  const boundedRatio = Math.min(Math.max(ratio, 0), 1)
  const expectedLongitude = interpolateLongitude(start.longitude, end.longitude, boundedRatio)
  const expectedLatitude = start.latitude + (end.latitude - start.latitude) * boundedRatio
  const latitudeRadians = (point.latitude * Math.PI) / 180
  const eastMetres =
    longitudeDelta(point.longitude, expectedLongitude) *
    111_320 *
    Math.max(Math.cos(latitudeRadians), 1e-6)
  const northMetres = (point.latitude - expectedLatitude) * 110_540
  let verticalMetres = 0
  if (
    point.altitudeMetres !== null &&
    start.altitudeMetres !== null &&
    end.altitudeMetres !== null
  ) {
    const expectedAltitude =
      start.altitudeMetres + (end.altitudeMetres - start.altitudeMetres) * boundedRatio
    verticalMetres = point.altitudeMetres - expectedAltitude
  }
  return eastMetres * eastMetres + northMetres * northMetres + verticalMetres * verticalMetres
}

function intervalCandidate(
  points: TrackPoint3D[],
  start: number,
  end: number,
): ErrorCandidate | null {
  if (end - start <= 1) return null
  let bestIndex = -1
  let bestErrorSquared = -1
  for (let index = start + 1; index < end; index += 1) {
    const errorSquared = interpolationErrorSquared(
      points[index]!,
      points[start]!,
      points[end]!,
      (index - start) / (end - start),
    )
    if (errorSquared > bestErrorSquared) {
      bestIndex = index
      bestErrorSquared = errorSquared
    }
  }
  return bestIndex < 0 ? null : { start, end, index: bestIndex, errorSquared: bestErrorSquared }
}

function semanticAnchorIndices(points: TrackPoint3D[]): number[] {
  const anchors = new Set<number>([0, points.length - 1])
  let worstErrorIndex = -1
  let worstError = -1
  for (let index = 1; index < points.length; index += 1) {
    if (points[index - 1]!.segment !== points[index]!.segment) {
      anchors.add(index - 1)
      anchors.add(index)
    }
  }
  for (let index = 0; index < points.length; index += 1) {
    const error = points[index]!.horizontalErrorMetres
    if (typeof error === 'number' && error > worstError) {
      worstError = error
      worstErrorIndex = index
    }
  }
  if (worstErrorIndex >= 0) anchors.add(worstErrorIndex)
  return [...anchors].sort((left, right) => left - right)
}

/**
 * 可复现的有界降采样：起终点与分段边界为强制锚点，其余点按相对于时间线性插值的
 * 三维误差从大到小加入。相同误差稳定选择较早的点；不会随机抽样。
 */
export function downsampleTrackPoints(
  points: TrackPoint3D[],
  maxPoints: number = DEFAULT_MAX_RENDER_POINTS_PER_TRACK,
): DownsampleResult {
  const budget = renderPointBudget({ maxPointsPerTrack: maxPoints })
  const orderedPoints = [...points].sort((left, right) => left.time - right.time)
  if (orderedPoints.length <= budget) {
    return { points: orderedPoints, semanticAnchorOverflow: false }
  }

  const anchors = semanticAnchorIndices(orderedPoints)
  const kept = new Set(anchors)
  const candidates: ErrorCandidate[] = []
  for (let index = 1; index < anchors.length; index += 1) {
    const candidate = intervalCandidate(orderedPoints, anchors[index - 1]!, anchors[index]!)
    if (candidate) candidates.push(candidate)
  }

  const effectiveBudget = Math.max(budget, anchors.length)
  while (kept.size < effectiveBudget && candidates.length > 0) {
    let bestPosition = 0
    for (let index = 1; index < candidates.length; index += 1) {
      const candidate = candidates[index]!
      const best = candidates[bestPosition]!
      if (
        candidate.errorSquared > best.errorSquared ||
        (candidate.errorSquared === best.errorSquared && candidate.index < best.index)
      ) {
        bestPosition = index
      }
    }
    const [best] = candidates.splice(bestPosition, 1)
    if (!best) break
    kept.add(best.index)
    const left = intervalCandidate(orderedPoints, best.start, best.index)
    const right = intervalCandidate(orderedPoints, best.index, best.end)
    if (left) candidates.push(left)
    if (right) candidates.push(right)
  }

  return {
    points: [...kept].sort((left, right) => left - right).map((index) => orderedPoints[index]!),
    semanticAnchorOverflow: anchors.length > budget,
  }
}

/**
 * 把 artifact 的全部 trajectory_2d 块归一化为可渲染轨迹。
 * - 默认只取有效时序；显式开启 includeUntimedGeometry 时保留无时间的静态几何；
 * - 同一 path 的多个分段合并为一条轨迹，分段值保留在每个点内；
 * - 同 time 的重复点去重（保留最后一个），并按 time 升序排序。
 */
export function buildTrajectoryRenderData(
  blocks: TrajectoryBlock[],
  options: TrajectoryRenderOptions = {},
): TrajectoryRenderData {
  const maxPointsPerTrack = renderPointBudget(options)
  const byPath = new Map<
    string,
    {
      pathValue: string
      snapshotId: string
      snapshotLabel: string
      label: string
      points: TrackPoint3D[]
      kinds: Set<TrackSegmentKind>
      timeBasis?: 'sequence'
    }
  >()

  blocks.forEach((block, blockIndex) => {
    const untimed = !block.time_key
    if (untimed && !options.includeUntimedGeometry) return
    const altitudeKey = findAltitudeKey(block)
    const groundSpeedKey = findGroundSpeedKey(block)
    const eastVelocityKey = findVelocityComponentKey(block, 'east')
    const northVelocityKey = findVelocityComponentKey(block, 'north')
    const verticalRateKey = findVelocityComponentKey(block, 'vertical')
    const horizontalErrorKey = findHorizontalErrorKey(block)
    for (const [index, raw] of (block.points ?? []).entries()) {
      const time = untimed ? index : toNumber(raw[block.time_key!])
      const longitude = toNumber(raw[block.x_key])
      const latitude = toNumber(raw[block.y_key])
      if (time === null || longitude === null || latitude === null) continue
      if (longitude < -180 || longitude > 180 || latitude < -90 || latitude > 90) continue
      // path_key 是点数据里的轨迹编号键名：按点的 path 值分组，
      // 跨块相同编号合并、块内不同编号拆分；缺编号的点归入该块兜底桶。
      const pathValue = block.path_key
        ? String(raw[block.path_key] ?? '') || `block-${blockIndex}`
        : `block-${blockIndex}`
      const snapshotId = block.snapshot_key
        ? String(raw[block.snapshot_key] ?? '') || DEFAULT_TRAJECTORY_SNAPSHOT_ID
        : DEFAULT_TRAJECTORY_SNAPSHOT_ID
      const bucketKey = `${snapshotId}\u0000${pathValue}${untimed ? '\u0000static' : ''}`
      const bucket = byPath.get(bucketKey) ?? {
        pathValue,
        snapshotId,
        snapshotLabel: snapshotId === DEFAULT_TRAJECTORY_SNAPSHOT_ID ? '默认快照' : snapshotId,
        label: block.path_label ?? block.title,
        points: [],
        kinds: new Set<TrackSegmentKind>(),
        ...(untimed ? { timeBasis: 'sequence' as const } : {}),
      }
      const segment = block.segment_key ? String(raw[block.segment_key] ?? '') : null
      const kind = segmentKind(segment)
      bucket.kinds.add(kind)
      bucket.points.push({
        time,
        longitude,
        latitude,
        altitudeMetres: altitudeKey ? toNumber(raw[altitudeKey]) : null,
        groundSpeedMps: groundSpeedKey ? toNumber(raw[groundSpeedKey]) : null,
        eastVelocityMps: eastVelocityKey ? toNumber(raw[eastVelocityKey]) : null,
        northVelocityMps: northVelocityKey ? toNumber(raw[northVelocityKey]) : null,
        verticalRateMps: verticalRateKey ? toNumber(raw[verticalRateKey]) : null,
        horizontalErrorMetres: horizontalErrorKey ? toNumber(raw[horizontalErrorKey]) : null,
        segment,
      })
      byPath.set(bucketKey, bucket)
    }
  })

  const tracks: Track3D[] = []
  let sourcePointCount = 0
  let renderedPointCount = 0
  let downsampledTrackCount = 0
  let semanticAnchorOverflowTrackCount = 0
  for (const bucket of byPath.values()) {
    const points = bucket.points.sort(
      (a, b) => a.time - b.time || a.segment?.localeCompare(b.segment ?? '') || 0,
    )
    const deduplicated: TrackPoint3D[] = []
    for (const point of points) {
      const previous = deduplicated[deduplicated.length - 1]
      if (previous && previous.time === point.time && previous.segment === point.segment) {
        deduplicated[deduplicated.length - 1] = point
      } else {
        deduplicated.push(point)
      }
    }
    if (deduplicated.length < (options.includeUntimedGeometry ? 1 : 2)) continue
    sourcePointCount += deduplicated.length
    const downsampled = downsampleTrackPoints(deduplicated, maxPointsPerTrack)
    renderedPointCount += downsampled.points.length
    if (downsampled.points.length < deduplicated.length) downsampledTrackCount += 1
    if (downsampled.semanticAnchorOverflow) semanticAnchorOverflowTrackCount += 1
    const kinds = bucket.kinds
    const kind: TrackSegmentKind =
      kinds.size === 1 ? ([...kinds][0] as TrackSegmentKind) : 'unknown'
    const presentation = trackPresentation(bucket.pathValue, bucket.label, kind)
    tracks.push({
      ...(bucket.timeBasis ? { timeBasis: bucket.timeBasis } : {}),
      id:
        bucket.snapshotId === DEFAULT_TRAJECTORY_SNAPSHOT_ID
          ? bucket.pathValue + (bucket.timeBasis ? '::static' : '')
          : `${bucket.snapshotId}::${bucket.pathValue}${bucket.timeBasis ? '::static' : ''}`,
      snapshotId: bucket.snapshotId,
      label: presentation.label,
      kind,
      color: presentation.color,
      points: downsampled.points,
    })
  }
  return {
    schemaVersion: TRAJECTORY_RENDER_SCHEMA_VERSION,
    tracks,
    snapshots: [...new Set(tracks.map((track) => track.snapshotId))].map((snapshotId) => ({
      id: snapshotId,
      label:
        [...byPath.values()].find((bucket) => bucket.snapshotId === snapshotId)?.snapshotLabel ??
        snapshotId,
      trackCount: tracks.filter((track) => track.snapshotId === snapshotId).length,
    })),
    downsampling: {
      algorithm: TRAJECTORY_DOWNSAMPLING_ALGORITHM,
      maxPointsPerTrack,
      sourcePointCount,
      renderedPointCount,
      downsampledTrackCount,
      semanticAnchorOverflowTrackCount,
    },
  }
}

/** 兼容旧调用方；新页面应消费 buildTrajectoryRenderData 的显式契约与统计。 */
export function normalizeTrajectoryBlocks(
  blocks: TrajectoryBlock[],
  options: TrajectoryRenderOptions = {},
): Track3D[] {
  return buildTrajectoryRenderData(blocks, options).tracks
}
