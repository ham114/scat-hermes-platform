import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { useTimeCursorStore } from '@/stores/timeCursor'
import type { Track3D, TrajectorySnapshot } from '@/visualization/types'
import TrajectoryPlaybackControls from './TrajectoryPlaybackControls.vue'

const tracks: Track3D[] = [
  {
    id: 'snapshot-a::actual',
    snapshotId: 'snapshot-a',
    label: '实际轨迹',
    kind: 'actual_future',
    color: '#16a34a',
    points: [
      {
        time: 10,
        longitude: 10,
        latitude: 50,
        altitudeMetres: 100,
        groundSpeedMps: 50,
        segment: 'actual',
      },
      {
        time: 20,
        longitude: 11,
        latitude: 51,
        altitudeMetres: 200,
        groundSpeedMps: 60,
        segment: 'actual',
      },
    ],
  },
]

const snapshots: TrajectorySnapshot[] = [
  { id: 'snapshot-a', label: '航班 1', trackCount: 5 },
  { id: 'snapshot-b', label: '航班 2', trackCount: 5 },
]

describe('TrajectoryPlaybackControls', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal(
      'requestAnimationFrame',
      vi.fn(() => 1),
    )
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
  })

  it('owns playback state and exposes the shared cursor controls', async () => {
    const wrapper = mount(TrajectoryPlaybackControls, {
      props: { tracks, snapshots, selectedSnapshotId: 'snapshot-a' },
      global: { plugins: [ElementPlus] },
    })
    const store = useTimeCursorStore()

    expect(store.tracks).toEqual(tracks)
    expect(store.currentTime).toBe(10)
    expect(wrapper.get('[data-testid="shared-time-readout"]').text()).toContain('00:00 / 00:10')

    await wrapper.get('[data-testid="playback-toggle"]').trigger('click')
    expect(store.playing).toBe(true)
    expect(wrapper.get('[data-testid="playback-toggle"]').text()).toBe('暂停')
  })

  it('emits snapshot changes and resets the cursor when tracks change', async () => {
    const wrapper = mount(TrajectoryPlaybackControls, {
      props: { tracks, snapshots, selectedSnapshotId: 'snapshot-a' },
      global: { plugins: [ElementPlus] },
    })
    const select = wrapper.findComponent({ name: 'ElSelect' })
    select.vm.$emit('update:modelValue', 'snapshot-b')
    await nextTick()

    expect(wrapper.emitted('update:selectedSnapshotId')).toEqual([['snapshot-b']])

    const shifted = tracks.map((track) => ({
      ...track,
      snapshotId: 'snapshot-b',
      points: track.points.map((point) => ({ ...point, time: point.time + 100 })),
    }))
    await wrapper.setProps({ tracks: shifted, selectedSnapshotId: 'snapshot-b' })
    expect(useTimeCursorStore().currentTime).toBe(110)
  })
})
