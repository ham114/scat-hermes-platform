import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useTimeCursorStore } from './timeCursor'
import type { Track3D } from '@/visualization/types'

function track(id: string, points: [number, number, number, number][]): Track3D {
  return {
    id,
    snapshotId: '__default__',
    label: id,
    kind: 'observed',
    color: '#2563eb',
    points: points.map(([time, longitude, latitude, altitudeMetres]) => ({
      time,
      longitude,
      latitude,
      altitudeMetres,
      groundSpeedMps: null,
      segment: null,
    })),
  }
}

describe('timeCursor store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('computes bounds from tracks and clamps the cursor', () => {
    const store = useTimeCursorStore()
    store.setTracks([
      track('a', [
        [10, 0, 0, 0],
        [20, 1, 1, 100],
      ]),
      track('b', [
        [30, 2, 2, 200],
        [40, 3, 3, 300],
      ]),
    ])

    expect(store.minTime).toBe(10)
    expect(store.maxTime).toBe(40)
    expect(store.duration).toBe(30)
    expect(store.currentTime).toBe(10)

    store.setTime(100)
    expect(store.currentTime).toBe(40)
    store.setTime(-5)
    expect(store.currentTime).toBe(10)
  })

  it('stops playing when the cursor reaches the end (single-pass semantics)', () => {
    const store = useTimeCursorStore()
    store.setTracks([
      track('a', [
        [0, 0, 0, 0],
        [10, 1, 1, 100],
      ]),
    ])
    store.play()
    expect(store.playing).toBe(true)

    store.advance(9)
    expect(store.currentTime).toBe(9)
    expect(store.playing).toBe(true)

    store.advance(5)
    expect(store.currentTime).toBe(10)
    expect(store.playing).toBe(false)
  })

  it('restarts from the beginning when playing after the end', () => {
    const store = useTimeCursorStore()
    store.setTracks([
      track('a', [
        [0, 0, 0, 0],
        [10, 1, 1, 100],
      ]),
    ])
    store.setTime(10)
    store.play()
    expect(store.currentTime).toBe(0)
    expect(store.playing).toBe(true)
  })

  it('rejects unsupported speeds and keeps the whitelist value', () => {
    const store = useTimeCursorStore()
    store.setSpeed(3)
    expect(store.speed).toBe(1)
    store.setSpeed(8)
    expect(store.speed).toBe(8)
  })

  it('is inert without tracks', () => {
    const store = useTimeCursorStore()
    store.play()
    expect(store.playing).toBe(false)
    store.advance(5)
    expect(store.currentTime).toBe(0)
  })
})
