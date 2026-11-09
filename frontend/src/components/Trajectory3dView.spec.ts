import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { useTimeCursorStore } from '@/stores/timeCursor'
import type { Track3D } from '@/visualization/types'
import Trajectory3dView from './Trajectory3dView.vue'

const visualizerMocks = vi.hoisted(() => ({
  instances: [] as Array<Record<string, ReturnType<typeof vi.fn>>>,
}))

vi.mock('@/visualization/CesiumTrajectoryVisualizer', () => {
  class MockVisualizer {
    mount = vi.fn()
    setTracks = vi.fn()
    setTime = vi.fn()
    setBasemap = vi.fn()
    setFollow = vi.fn()
    setFollowTarget = vi.fn()
    setAltitudeScale = vi.fn()
    setViewPreset = vi.fn()
    setHighlightTrack = vi.fn()
    dispose = vi.fn()

    constructor() {
      visualizerMocks.instances.push(this as unknown as Record<string, ReturnType<typeof vi.fn>>)
    }
  }
  return {
    BASEMAPS: [
      { id: 'esri', label: 'Esri', maxLevel: 19 },
      { id: 'amap', label: '高德', maxLevel: 18 },
    ],
    CesiumTrajectoryVisualizer: MockVisualizer,
  }
})

const tracks: Track3D[] = [
  {
    id: 'snapshot::actual',
    snapshotId: 'snapshot',
    label: '实际轨迹',
    kind: 'actual_future',
    color: '#16a34a',
    points: [
      {
        time: 10,
        longitude: 10,
        latitude: 50,
        altitudeMetres: 1_000,
        groundSpeedMps: 100,
        segment: 'actual_future',
      },
      {
        time: 20,
        longitude: 11,
        latitude: 51,
        altitudeMetres: 2_000,
        groundSpeedMps: 120,
        segment: 'actual_future',
      },
    ],
  },
  {
    id: 'snapshot::kalman-cv',
    snapshotId: 'snapshot',
    label: 'Kalman 常速度',
    kind: 'predicted',
    color: '#8b5cf6',
    points: [
      {
        time: 10,
        longitude: 10,
        latitude: 50,
        altitudeMetres: null,
        groundSpeedMps: 100,
        segment: 'predicted',
      },
      {
        time: 20,
        longitude: 11,
        latitude: 51,
        altitudeMetres: null,
        groundSpeedMps: 120,
        segment: 'predicted',
      },
    ],
  },
]

describe('Trajectory3dView', () => {
  beforeEach(() => {
    visualizerMocks.instances.length = 0
    setActivePinia(createPinia())
    useTimeCursorStore().setTracks(tracks)
  })

  it('shows a 3D-specific color legend with raw current altitude', async () => {
    useTimeCursorStore().setTime(15)
    const wrapper = mount(Trajectory3dView, {
      props: { tracks },
      global: { plugins: [ElementPlus] },
    })
    await nextTick()

    const legend = wrapper.get('[aria-label="三维轨迹图例"]')
    expect(legend.text()).toContain('实际轨迹')
    expect(legend.text()).toContain('1,500 m')
    expect(legend.text()).toContain('Kalman 常速度')
    expect(legend.text()).toContain('高度未知')
    const attitude = wrapper.get('[aria-label="当前姿态估计"]')
    expect(attitude.text()).toContain('实际轨迹 · 姿态估计')
    expect(attitude.text()).toContain('航向')
    expect(attitude.text()).toContain('俯仰')
    expect(attitude.text()).toContain('滚转')
    expect(attitude.text()).toContain('仅用于回放')
    expect(visualizerMocks.instances[0]!.setFollowTarget).toHaveBeenCalledWith('snapshot::actual')
  })

  it('forwards height, camera and selected follow target controls', async () => {
    const wrapper = mount(Trajectory3dView, {
      props: { tracks },
      global: { plugins: [ElementPlus] },
    })
    await nextTick()
    const visualizer = visualizerMocks.instances[0]!
    const selects = wrapper.findAllComponents({ name: 'ElSelect' })

    selects[0]!.vm.$emit('update:modelValue', 'snapshot::kalman-cv')
    selects[2]!.vm.$emit('update:modelValue', 5)
    await nextTick()
    expect(visualizer.setFollowTarget).toHaveBeenLastCalledWith('snapshot::kalman-cv')
    expect(visualizer.setAltitudeScale).toHaveBeenLastCalledWith(5)
    expect(wrapper.text()).toContain('高度显示已放大 5×')

    await wrapper.findAllComponents({ name: 'ElButton' })[0]!.trigger('click')
    expect(visualizer.setFollow).toHaveBeenLastCalledWith(true)

    selects[1]!.vm.$emit('update:modelValue', 'side')
    await nextTick()
    expect(visualizer.setFollow).toHaveBeenLastCalledWith(false)
    expect(visualizer.setViewPreset).toHaveBeenLastCalledWith('side')
  })

  it('places playback controls immediately below the replay canvas', () => {
    const wrapper = mount(Trajectory3dView, {
      props: { tracks },
      slots: { playback: '<div data-testid="playback-slot-content">时间控制</div>' },
      global: { plugins: [ElementPlus] },
    })

    const children = Array.from(wrapper.element.children)
    const mapIndex = children.indexOf(wrapper.get('.map-shell').element)
    const playbackIndex = children.indexOf(wrapper.get('.playback-slot').element)
    const viewBarIndex = children.indexOf(wrapper.get('.view-bar').element)
    expect(mapIndex).toBeLessThan(playbackIndex)
    expect(playbackIndex).toBeLessThan(viewBarIndex)
  })
})
