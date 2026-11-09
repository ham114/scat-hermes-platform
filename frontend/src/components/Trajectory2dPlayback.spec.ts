import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import { defineComponent, nextTick } from 'vue'

import { useTimeCursorStore } from '@/stores/timeCursor'
import type { Track3D } from '@/visualization/types'
import Trajectory2dPlayback from './Trajectory2dPlayback.vue'

const BaseChartStub = defineComponent({
  name: 'BaseChart',
  props: {
    option: { type: Object, required: true },
    height: { type: String, default: '320px' },
    label: { type: String, default: 'chart' },
  },
  template: '<div class="base-chart-stub" />',
})

const tracks: Track3D[] = [
  {
    id: 'flight-1',
    snapshotId: '__default__',
    label: 'flight-1',
    kind: 'observed',
    color: '#2563eb',
    points: [
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
        groundSpeedMps: 70,
        segment: 'observed',
      },
    ],
  },
]

describe('Trajectory2dPlayback', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders full path, elapsed trail and current marker from the shared cursor', async () => {
    const wrapper = mount(Trajectory2dPlayback, {
      props: { tracks },
      global: { stubs: { BaseChart: BaseChartStub } },
    })
    const store = useTimeCursorStore()
    store.setTracks(tracks)
    store.setTime(5)
    await nextTick()

    const option = wrapper.findComponent(BaseChartStub).props('option') as {
      legend: { data: string[] }
      series: Array<{ type: string; data: Array<[number, number]> }>
    }
    expect(option.legend.data).toEqual(['flight-1 · 已播放'])
    expect(option.series).toHaveLength(3)
    expect(option.series[0]!.data).toEqual([
      [10, 50],
      [12, 52],
    ])
    expect(option.series[1]!.data).toEqual([
      [10, 50],
      [11, 51],
    ])
    expect(option.series[2]!.type).toBe('scatter')
    expect(option.series[2]!.data).toEqual([[11, 51]])
    expect(wrapper.get('[data-testid="two-dimensional-cursor"]').text()).toContain('00:05 / 00:10')
  })

  it('updates the chart when the global cursor changes', async () => {
    const wrapper = mount(Trajectory2dPlayback, {
      props: { tracks },
      global: { stubs: { BaseChart: BaseChartStub } },
    })
    const store = useTimeCursorStore()
    store.setTracks(tracks)
    store.setTime(10)
    await nextTick()

    const option = wrapper.findComponent(BaseChartStub).props('option') as {
      series: Array<{ data: Array<[number, number]> }>
    }
    expect(option.series[1]!.data).toEqual([
      [10, 50],
      [12, 52],
    ])
  })
})
