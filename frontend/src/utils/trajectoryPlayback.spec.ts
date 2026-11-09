import { describe, expect, it } from 'vitest'

import type { Track3D } from '@/visualization/types'
import {
  buildTrackPlayback2D,
  estimateAircraftAttitude,
  interpolateTrackAtTime,
} from './trajectoryPlayback'

function track(points: Track3D['points']): Track3D {
  return {
    id: 'flight-1',
    snapshotId: '__default__',
    label: 'flight-1',
    kind: 'observed',
    color: '#2563eb',
    points,
  }
}

describe('trajectoryPlayback', () => {
  it('interpolates position and altitude on the shared time axis', () => {
    const value = interpolateTrackAtTime(
      track([
        {
          time: 0,
          longitude: 10,
          latitude: 50,
          altitudeMetres: 100,
          groundSpeedMps: 50,
          segment: 'observed',
        },
        {
          time: 10,
          longitude: 12,
          latitude: 52,
          altitudeMetres: 300,
          groundSpeedMps: 150,
          segment: 'observed',
        },
      ]),
      5,
    )

    expect(value).toEqual({
      time: 5,
      longitude: 11,
      latitude: 51,
      altitudeMetres: 200,
      groundSpeedMps: 100,
      eastVelocityMps: null,
      northVelocityMps: null,
      verticalRateMps: null,
      horizontalErrorMetres: null,
      segment: 'observed',
    })
  })

  it('estimates heading and pitch from the six-feature velocity components', () => {
    const value = track([
      {
        time: 0,
        longitude: 10,
        latitude: 50,
        altitudeMetres: 1_000,
        groundSpeedMps: 100,
        eastVelocityMps: 100,
        northVelocityMps: 0,
        verticalRateMps: 10,
        segment: 'predicted',
      },
      {
        time: 10,
        longitude: 10.014,
        latitude: 50,
        altitudeMetres: 1_100,
        groundSpeedMps: 100,
        eastVelocityMps: 100,
        northVelocityMps: 0,
        verticalRateMps: 10,
        segment: 'predicted',
      },
    ])

    const attitude = estimateAircraftAttitude(value, 5)!
    expect(attitude.source).toBe('velocity')
    expect(attitude.headingRadians).toBeCloseTo(Math.PI / 2)
    expect(attitude.pitchRadians).toBeCloseTo(Math.atan2(10, 100))
    expect(attitude.rollRadians).toBeCloseTo(0)
  })

  it('falls back to trajectory geometry and estimates coordinated-turn roll', () => {
    const value = track([
      {
        time: 0,
        longitude: 0,
        latitude: 0,
        altitudeMetres: 1_000,
        groundSpeedMps: 150,
        segment: 'predicted',
      },
      {
        time: 10,
        longitude: 0.01,
        latitude: 0,
        altitudeMetres: 1_000,
        groundSpeedMps: 150,
        segment: 'predicted',
      },
      {
        time: 20,
        longitude: 0.01,
        latitude: -0.01,
        altitudeMetres: 1_000,
        groundSpeedMps: 150,
        segment: 'predicted',
      },
    ])

    const attitude = estimateAircraftAttitude(value, 10)!
    expect(attitude.source).toBe('geometry')
    expect(attitude.rollRadians).toBeGreaterThan(0)
    expect(attitude.rollRadians).toBeLessThanOrEqual(Math.PI / 3)
  })

  it('uses the shortest longitude arc across the antimeridian', () => {
    const value = interpolateTrackAtTime(
      track([
        {
          time: 0,
          longitude: 179,
          latitude: 0,
          altitudeMetres: null,
          groundSpeedMps: null,
          segment: null,
        },
        {
          time: 10,
          longitude: -179,
          latitude: 0,
          altitudeMetres: null,
          groundSpeedMps: null,
          segment: null,
        },
      ]),
      5,
    )

    expect(Math.abs(value!.longitude)).toBe(180)
  })

  it('builds an elapsed trail ending at the interpolated current point', () => {
    const value = buildTrackPlayback2D(
      track([
        {
          time: 0,
          longitude: 10,
          latitude: 50,
          altitudeMetres: null,
          groundSpeedMps: null,
          segment: null,
        },
        {
          time: 10,
          longitude: 12,
          latitude: 52,
          altitudeMetres: null,
          groundSpeedMps: null,
          segment: null,
        },
      ]),
      5,
    )

    expect(value.fullPath).toEqual([
      [10, 50],
      [12, 52],
    ])
    expect(value.trail).toEqual([
      [10, 50],
      [11, 51],
    ])
    expect(value.current?.time).toBe(5)
  })

  it('returns no current point before a track begins and clamps after it ends', () => {
    const value = track([
      {
        time: 10,
        longitude: 1,
        latitude: 2,
        altitudeMetres: null,
        groundSpeedMps: null,
        segment: null,
      },
      {
        time: 20,
        longitude: 3,
        latitude: 4,
        altitudeMetres: null,
        groundSpeedMps: null,
        segment: null,
      },
    ])

    expect(interpolateTrackAtTime(value, 5)).toBeNull()
    expect(interpolateTrackAtTime(value, 30)?.time).toBe(20)
  })

  it('uses the last point when timestamps are duplicated', () => {
    const value = interpolateTrackAtTime(
      track([
        {
          time: 0,
          longitude: 1,
          latitude: 2,
          altitudeMetres: null,
          groundSpeedMps: null,
          segment: 'observed',
        },
        {
          time: 0,
          longitude: 3,
          latitude: 4,
          altitudeMetres: null,
          groundSpeedMps: null,
          segment: 'predicted',
        },
        {
          time: 10,
          longitude: 5,
          latitude: 6,
          altitudeMetres: null,
          groundSpeedMps: null,
          segment: 'predicted',
        },
      ]),
      0,
    )

    expect(value?.longitude).toBe(3)
    expect(value?.segment).toBe('predicted')
  })
})
