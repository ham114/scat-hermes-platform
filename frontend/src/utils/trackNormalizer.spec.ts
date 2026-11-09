import { describe, expect, it } from 'vitest'

import type { components } from '@/api/schema'
import {
  buildTrajectoryRenderData,
  downsampleTrackPoints,
  normalizeTrajectoryBlocks,
} from './trackNormalizer'
import type { TrackPoint3D } from '@/visualization/types'

type TrajectoryBlock = components['schemas']['Trajectory2dBlock']

function block(overrides: Partial<TrajectoryBlock>): TrajectoryBlock {
  return {
    type: 'trajectory_2d',
    title: '测试轨迹',
    x_key: 'longitude',
    x_label: '经度',
    y_key: 'latitude',
    y_label: '纬度',
    time_key: 'time_step_s',
    series: [{ key: 'altitude', label: '高度', unit: 'm', role: 'color' }],
    points: [],
    ...overrides,
  }
}

describe('trackNormalizer', () => {
  it('normalizes one trajectory block into a track', () => {
    const track = normalizeTrajectoryBlocks([
      block({
        path_key: 'path',
        path_label: '100002',
        series: [
          { key: 'altitude', label: '高度', unit: 'm', role: 'color' },
          { key: 'ground_speed', label: '水平地速', unit: 'm/s', role: 'size' },
          { key: 'horizontal_error_m', label: '水平误差', unit: 'm', role: 'color' },
        ],
        points: [
          {
            time_step_s: 0,
            longitude: 10,
            latitude: 55,
            altitude: 100,
            ground_speed: 50,
            horizontal_error_m: 120,
            path: '100002',
          },
          {
            time_step_s: 10,
            longitude: 11,
            latitude: 56,
            altitude: 200,
            ground_speed: 60,
            horizontal_error_m: 240,
            path: '100002',
          },
        ],
      }),
    ])

    expect(track).toHaveLength(1)
    expect(track[0]!.id).toBe('100002')
    expect(track[0]!.snapshotId).toBe('__default__')
    expect(track[0]!.points.map((point) => point.altitudeMetres)).toEqual([100, 200])
    expect(track[0]!.points.map((point) => point.groundSpeedMps)).toEqual([50, 60])
    expect(track[0]!.points.map((point) => point.horizontalErrorMetres)).toEqual([120, 240])
  })

  it('skips blocks without a time key and invalid points', () => {
    const tracks = normalizeTrajectoryBlocks([
      block({ time_key: null, points: [{ longitude: 1, latitude: 2 }] }),
      block({
        points: [
          { time_step_s: 0, longitude: 10, latitude: 55, altitude: 100 },
          { time_step_s: 5, longitude: 'broken', latitude: 55, altitude: 120 },
          { time_step_s: 10, longitude: 12, latitude: 57, altitude: 140 },
        ],
      }),
    ])

    expect(tracks).toHaveLength(1)
    expect(tracks[0]!.points).toHaveLength(2)
    expect(tracks[0]!.points[1]!.longitude).toBe(12)
  })

  it('retains untimed geometry only when explicitly enabled and never invents seconds', () => {
    const source = block({
      time_key: null,
      x_unit: 'deg',
      y_unit: 'deg',
      points: [
        { longitude: 18, latitude: 59, altitude: 100 },
        { longitude: 18.1, latitude: 59.1, altitude: 150 },
      ],
    })
    expect(buildTrajectoryRenderData([source]).tracks).toEqual([])
    const result = buildTrajectoryRenderData([source], { includeUntimedGeometry: true })
    expect(result.tracks).toHaveLength(1)
    expect(result.tracks[0]!.timeBasis).toBe('sequence')
    expect(result.tracks[0]!.points.map((p) => p.longitude)).toEqual([18, 18.1])
    expect(source.time_key).toBeNull()
    expect(source.points![0]).not.toHaveProperty('time')
  })

  it('allows one static point and rejects invalid geographic coordinates', () => {
    const result = buildTrajectoryRenderData(
      [
        block({
          time_key: null,
          points: [
            { longitude: 18, latitude: 59 },
            { longitude: 18, latitude: 91 },
            { longitude: '', latitude: 59 },
            { longitude: 181, latitude: 59 },
          ],
        }),
      ],
      { includeUntimedGeometry: true },
    )
    expect(result.tracks[0]!.points).toHaveLength(1)
    expect(result.downsampling.renderedPointCount).toBe(1)
  })

  it('maps optional GBAR velocity features without mistaking vx for ground speed', () => {
    const tracks = normalizeTrajectoryBlocks([
      block({
        series: [
          { key: 'altitude_m', label: '高度', unit: 'm', role: 'color' },
          { key: 'vx', label: '东向速度', unit: 'm/s', role: 'size' },
          { key: 'vy', label: '北向速度', unit: 'm/s', role: 'size' },
          { key: 'rocd', label: '垂直速度', unit: 'm/s', role: 'size' },
        ],
        points: [
          { time_step_s: 0, longitude: 10, latitude: 55, altitude_m: 100, vx: 80, vy: 60, rocd: 2 },
          {
            time_step_s: 10,
            longitude: 11,
            latitude: 56,
            altitude_m: 120,
            vx: 82,
            vy: 61,
            rocd: 2,
          },
        ],
      }),
    ])

    expect(tracks[0]!.points[0]).toMatchObject({
      groundSpeedMps: null,
      eastVelocityMps: 80,
      northVelocityMps: 60,
      verticalRateMps: 2,
    })
  })

  it('merges segments of the same path into one track and maps kinds', () => {
    const tracks = normalizeTrajectoryBlocks([
      block({
        path_key: 'path',
        path_label: '100002',
        segment_key: 'segment',
        segment_label: '分段',
        points: [
          { time_step_s: 0, longitude: 10, latitude: 55, segment: 'observed', path: '100002' },
          { time_step_s: 10, longitude: 11, latitude: 56, segment: 'observed', path: '100002' },
        ],
      }),
      block({
        path_key: 'path',
        path_label: '100002',
        segment_key: 'segment',
        segment_label: '分段',
        points: [
          { time_step_s: 10, longitude: 11, latitude: 56, segment: 'predicted', path: '100002' },
          { time_step_s: 20, longitude: 12, latitude: 57, segment: 'predicted', path: '100002' },
        ],
      }),
    ])

    expect(tracks).toHaveLength(1)
    expect(tracks[0]!.points).toHaveLength(4)
    expect(tracks[0]!.kind).toBe('unknown')
    expect(tracks[0]!.color).toBe('#9ca3af')
  })

  it('assigns stable labels and distinct colors to actual and comparison methods', () => {
    const points = [
      'actual',
      'scat-gbar-v23',
      'kalman-cv',
      'constant-velocity-heading',
      'cfpl',
    ].flatMap((path, pathIndex) => [
      {
        time_step_s: 10,
        longitude: 10 + pathIndex,
        latitude: 50,
        path,
        segment: path === 'actual' ? 'actual_future' : 'predicted',
      },
      {
        time_step_s: 20,
        longitude: 11 + pathIndex,
        latitude: 51,
        path,
        segment: path === 'actual' ? 'actual_future' : 'predicted',
      },
    ])
    const tracks = normalizeTrajectoryBlocks([
      block({
        path_key: 'path',
        path_label: '轨迹方法',
        segment_key: 'segment',
        segment_label: '轨迹类型',
        points,
      }),
    ])

    expect(tracks.map((track) => track.label)).toEqual([
      '实际轨迹',
      'GBAR v23',
      'Kalman 常速度',
      '匀速-恒航向',
      'CFPL 计划航路',
    ])
    expect(new Set(tracks.map((track) => track.color)).size).toBe(5)
    expect(
      tracks.every((track) => track.points.map((point) => point.time).join() === '10,20'),
    ).toBe(true)
  })

  it('keeps identical method paths separate across switchable snapshots', () => {
    const renderData = buildTrajectoryRenderData([
      block({
        path_key: 'path',
        path_label: '方法',
        snapshot_key: 'snapshot',
        snapshot_label: '快照',
        segment_key: 'segment',
        segment_label: '类型',
        points: ['flight-1 @ t1', 'flight-2 @ t2'].flatMap((snapshot, index) => [
          {
            time_step_s: 10,
            longitude: 10 + index,
            latitude: 50,
            path: 'kalman-cv',
            snapshot,
            segment: 'predicted',
          },
          {
            time_step_s: 20,
            longitude: 11 + index,
            latitude: 51,
            path: 'kalman-cv',
            snapshot,
            segment: 'predicted',
          },
        ]),
      }),
    ])

    expect(renderData.snapshots).toEqual([
      { id: 'flight-1 @ t1', label: 'flight-1 @ t1', trackCount: 1 },
      { id: 'flight-2 @ t2', label: 'flight-2 @ t2', trackCount: 1 },
    ])
    expect(renderData.tracks).toHaveLength(2)
    expect(renderData.tracks.map((track) => track.id)).toEqual([
      'flight-1 @ t1::kalman-cv',
      'flight-2 @ t2::kalman-cv',
    ])
  })

  it('sorts by time and deduplicates identical (time, segment) pairs', () => {
    const tracks = normalizeTrajectoryBlocks([
      block({
        points: [
          { time_step_s: 10, longitude: 11, latitude: 56 },
          { time_step_s: 0, longitude: 10, latitude: 55 },
          { time_step_s: 10, longitude: 12, latitude: 57 },
        ],
      }),
    ])

    expect(tracks[0]!.points.map((point) => point.time)).toEqual([0, 10])
    expect(tracks[0]!.points[1]!.longitude).toBe(12)
  })

  it('rejects tracks with fewer than two valid points', () => {
    const tracks = normalizeTrajectoryBlocks([
      block({ points: [{ time_step_s: 0, longitude: 10, latitude: 55 }] }),
    ])

    expect(tracks).toHaveLength(0)
  })

  it('builds one versioned render contract shared by every visualizer', () => {
    const renderData = buildTrajectoryRenderData(
      [
        block({
          points: [
            { time_step_s: 0, longitude: 0, latitude: 0, altitude: 0 },
            { time_step_s: 1, longitude: 1, latitude: 0, altitude: 0 },
            { time_step_s: 2, longitude: 2, latitude: 5, altitude: 500 },
            { time_step_s: 3, longitude: 3, latitude: 0, altitude: 0 },
            { time_step_s: 4, longitude: 4, latitude: 0, altitude: 0 },
          ],
        }),
      ],
      { maxPointsPerTrack: 3 },
    )

    expect(renderData.schemaVersion).toBe('trajectory-render-v1')
    expect(renderData.downsampling).toMatchObject({
      algorithm: 'segment-aware-spatiotemporal-max-error-v1',
      maxPointsPerTrack: 3,
      sourcePointCount: 5,
      renderedPointCount: 3,
      downsampledTrackCount: 1,
    })
    expect(renderData.tracks[0]!.points.map((point) => point.time)).toEqual([0, 2, 4])
  })

  it('preserves endpoints and both sides of every segment boundary', () => {
    const points: TrackPoint3D[] = [
      {
        time: 0,
        longitude: 0,
        latitude: 0,
        altitudeMetres: 0,
        groundSpeedMps: null,
        segment: 'observed',
      },
      {
        time: 1,
        longitude: 1,
        latitude: 0,
        altitudeMetres: 0,
        groundSpeedMps: null,
        segment: 'observed',
      },
      {
        time: 2,
        longitude: 2,
        latitude: 0,
        altitudeMetres: 0,
        groundSpeedMps: null,
        segment: 'observed',
      },
      {
        time: 3,
        longitude: 3,
        latitude: 0,
        altitudeMetres: 0,
        groundSpeedMps: null,
        segment: 'predicted',
      },
      {
        time: 4,
        longitude: 4,
        latitude: 0,
        altitudeMetres: 0,
        groundSpeedMps: null,
        segment: 'predicted',
      },
      {
        time: 5,
        longitude: 5,
        latitude: 0,
        altitudeMetres: 0,
        groundSpeedMps: null,
        segment: 'predicted',
      },
    ]

    const result = downsampleTrackPoints(points, 2)

    expect(result.points.map((point) => point.time)).toEqual([0, 2, 3, 5])
    expect(result.semanticAnchorOverflow).toBe(true)
  })

  it('is deterministic for a representative large track and retains its key turn', () => {
    const points: TrackPoint3D[] = Array.from({ length: 10_000 }, (_, index) => ({
      time: index,
      longitude: 10 + index * 0.0001,
      latitude: 50 + (index === 5_000 ? 1 : 0),
      altitudeMetres: 1_000 + (index === 7_500 ? 2_000 : 0),
      groundSpeedMps: null,
      segment: 'observed',
    }))

    const first = downsampleTrackPoints(points, 128)
    const second = downsampleTrackPoints(points, 128)

    expect(first.points).toHaveLength(128)
    expect(first.points[0]).toBe(points[0])
    expect(first.points.at(-1)).toBe(points.at(-1))
    expect(first.points).toContain(points[5_000])
    expect(first.points).toContain(points[7_500])
    expect(first.points.map((point) => point.time)).toEqual(
      second.points.map((point) => point.time),
    )
  })

  it('uses the shortest longitude arc when ranking dateline-crossing points', () => {
    const points: TrackPoint3D[] = [
      {
        time: 0,
        longitude: 179,
        latitude: 0,
        altitudeMetres: null,
        groundSpeedMps: null,
        segment: null,
      },
      {
        time: 1,
        longitude: -180,
        latitude: 0,
        altitudeMetres: null,
        groundSpeedMps: null,
        segment: null,
      },
      {
        time: 2,
        longitude: -179,
        latitude: 0,
        altitudeMetres: null,
        groundSpeedMps: null,
        segment: null,
      },
      {
        time: 3,
        longitude: -178,
        latitude: 1,
        altitudeMetres: null,
        groundSpeedMps: null,
        segment: null,
      },
      {
        time: 4,
        longitude: -177,
        latitude: 0,
        altitudeMetres: null,
        groundSpeedMps: null,
        segment: null,
      },
    ]

    const result = downsampleTrackPoints(points, 3)

    expect(result.points.map((point) => point.time)).toEqual([0, 3, 4])
  })

  it('retains the backend-declared worst error point as a semantic anchor', () => {
    const points: TrackPoint3D[] = Array.from({ length: 20 }, (_, index) => ({
      time: index,
      longitude: 10 + index,
      latitude: 50,
      altitudeMetres: null,
      groundSpeedMps: null,
      horizontalErrorMetres: index === 9 ? 9_999 : index,
      segment: 'predicted',
    }))

    const result = downsampleTrackPoints(points, 2)

    expect(result.points.map((point) => point.time)).toEqual([0, 9, 19])
    expect(result.semanticAnchorOverflow).toBe(true)
  })

  it('rejects an invalid render budget', () => {
    expect(() => buildTrajectoryRenderData([], { maxPointsPerTrack: 1 })).toThrow(RangeError)
  })
})
