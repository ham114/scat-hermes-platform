import type { AircraftAttitudeEstimate, Track3D, TrackPoint3D } from '@/visualization/types'

export interface TrackPlayback2D {
  fullPath: Array<[number, number]>
  trail: Array<[number, number]>
  current: TrackPoint3D | null
}

function wrapLongitude(value: number): number {
  return ((((value + 180) % 360) + 360) % 360) - 180
}

function interpolateLongitude(start: number, end: number, ratio: number): number {
  const delta = ((end - start + 540) % 360) - 180
  return wrapLongitude(start + delta * ratio)
}

function interpolateOptional(
  start: number | null,
  end: number | null,
  ratio: number,
): number | null {
  if (start === null || end === null) return start ?? end
  return start + (end - start) * ratio
}

const EARTH_RADIUS_METRES = 6_371_008.8
const GRAVITY_METRES_PER_SECOND_SQUARED = 9.80665

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum)
}

function wrapRadians(value: number): number {
  return ((((value + Math.PI) % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI)) - Math.PI
}

function segmentMotion(start: TrackPoint3D, end: TrackPoint3D) {
  const latitude1 = (start.latitude * Math.PI) / 180
  const latitude2 = (end.latitude * Math.PI) / 180
  const deltaLatitude = latitude2 - latitude1
  const deltaLongitude = (((end.longitude - start.longitude + 540) % 360) - 180) * (Math.PI / 180)
  const east = Math.sin(deltaLongitude) * Math.cos(latitude2)
  const north =
    Math.cos(latitude1) * Math.sin(latitude2) -
    Math.sin(latitude1) * Math.cos(latitude2) * Math.cos(deltaLongitude)
  const heading = Math.atan2(east, north)
  const haversine =
    Math.sin(deltaLatitude / 2) ** 2 +
    Math.cos(latitude1) * Math.cos(latitude2) * Math.sin(deltaLongitude / 2) ** 2
  const distance =
    2 * EARTH_RADIUS_METRES * Math.asin(Math.min(1, Math.sqrt(Math.max(0, haversine))))
  const duration = Math.max(end.time - start.time, Number.EPSILON)
  const verticalRate =
    start.altitudeMetres === null || end.altitudeMetres === null
      ? 0
      : (end.altitudeMetres - start.altitudeMetres) / duration
  return { heading, horizontalSpeed: distance / duration, verticalRate, duration }
}

/**
 * 用 GBAR 六特征或统一轨迹几何估算展示姿态。
 * 这不是机载 IMU 姿态：滚转采用协调转弯假设，且三轴均做显示安全限幅。
 */
export function estimateAircraftAttitude(
  track: Track3D,
  time: number,
): AircraftAttitudeEstimate | null {
  if (track.timeBasis === 'sequence') return null
  const points = [...new Map(track.points.map((point) => [point.time, point])).values()].sort(
    (left, right) => left.time - right.time,
  )
  if (points.length < 2) return null

  let segmentIndex = points.findIndex((point) => point.time > time) - 1
  if (segmentIndex < 0) segmentIndex = 0
  if (segmentIndex >= points.length - 1) segmentIndex = points.length - 2
  const start = points[segmentIndex]!
  const end = points[segmentIndex + 1]!
  const segment = segmentMotion(start, end)
  const current = interpolateTrackAtTime(track, clamp(time, points[0]!.time, points.at(-1)!.time))

  const eastVelocity = current?.eastVelocityMps
  const northVelocity = current?.northVelocityMps
  const featureHorizontalSpeed =
    typeof eastVelocity === 'number' && typeof northVelocity === 'number'
      ? Math.hypot(eastVelocity, northVelocity)
      : null
  const usesVelocity = featureHorizontalSpeed !== null && featureHorizontalSpeed > 0.1
  const heading = usesVelocity ? Math.atan2(eastVelocity!, northVelocity!) : segment.heading
  const horizontalSpeed = usesVelocity
    ? featureHorizontalSpeed
    : (current?.groundSpeedMps ?? segment.horizontalSpeed)
  const verticalRate = current?.verticalRateMps ?? segment.verticalRate
  const pitch = clamp(
    Math.atan2(verticalRate, Math.max(horizontalSpeed, 0.1)),
    -Math.PI / 4,
    Math.PI / 4,
  )

  const previousSegment =
    segmentIndex > 0 ? segmentMotion(points[segmentIndex - 1]!, start) : segment
  const nextSegment =
    segmentIndex + 2 < points.length ? segmentMotion(end, points[segmentIndex + 2]!) : segment
  const yawDuration = Math.max((previousSegment.duration + nextSegment.duration) / 2, 1)
  const yawRate = clamp(
    wrapRadians(nextSegment.heading - previousSegment.heading) / yawDuration,
    -0.15,
    0.15,
  )
  const roll = clamp(
    Math.atan((Math.max(horizontalSpeed, 0) * yawRate) / GRAVITY_METRES_PER_SECOND_SQUARED),
    -Math.PI / 3,
    Math.PI / 3,
  )

  return {
    headingRadians: wrapRadians(heading),
    pitchRadians: pitch,
    rollRadians: roll,
    source: usesVelocity ? 'velocity' : 'geometry',
  }
}

/**
 * Resolve the deterministic position at one shared playback time.
 * Points with duplicate timestamps use the last value, matching normalization.
 */
export function interpolateTrackAtTime(track: Track3D, time: number): TrackPoint3D | null {
  const byTime = new Map<number, TrackPoint3D>()
  for (const point of track.points) byTime.set(point.time, point)
  const points = [...byTime.values()].sort((left, right) => left.time - right.time)
  if (points.length === 0 || time < points[0]!.time) return null
  if (time >= points[points.length - 1]!.time) return points[points.length - 1]!

  const exact = byTime.get(time)
  if (exact) return exact
  const afterIndex = points.findIndex((point) => point.time > time)
  if (afterIndex <= 0) return null
  const before = points[afterIndex - 1]!
  const after = points[afterIndex]!
  const span = after.time - before.time
  if (span <= 0) return before
  const ratio = (time - before.time) / span
  return {
    time,
    longitude: interpolateLongitude(before.longitude, after.longitude, ratio),
    latitude: before.latitude + (after.latitude - before.latitude) * ratio,
    altitudeMetres: interpolateOptional(before.altitudeMetres, after.altitudeMetres, ratio),
    groundSpeedMps: interpolateOptional(before.groundSpeedMps, after.groundSpeedMps, ratio),
    eastVelocityMps: interpolateOptional(
      before.eastVelocityMps ?? null,
      after.eastVelocityMps ?? null,
      ratio,
    ),
    northVelocityMps: interpolateOptional(
      before.northVelocityMps ?? null,
      after.northVelocityMps ?? null,
      ratio,
    ),
    verticalRateMps: interpolateOptional(
      before.verticalRateMps ?? null,
      after.verticalRateMps ?? null,
      ratio,
    ),
    horizontalErrorMetres: interpolateOptional(
      before.horizontalErrorMetres ?? null,
      after.horizontalErrorMetres ?? null,
      ratio,
    ),
    segment: before.segment ?? after.segment,
  }
}

/** Build the complete path, elapsed trail and current marker for one track. */
export function buildTrackPlayback2D(track: Track3D, time: number): TrackPlayback2D {
  const current = interpolateTrackAtTime(track, time)
  const trail = track.points
    .filter((point) => point.time <= time)
    .map((point): [number, number] => [point.longitude, point.latitude])
  if (current) {
    const last = trail[trail.length - 1]
    if (!last || last[0] !== current.longitude || last[1] !== current.latitude) {
      trail.push([current.longitude, current.latitude])
    }
  }
  return {
    fullPath: track.points.map((point) => [point.longitude, point.latitude]),
    trail,
    current,
  }
}
