import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import { defineComponent, nextTick } from 'vue'

import { useTimeCursorStore } from '@/stores/timeCursor'
import type { Track3D } from '@/visualization/types'
import TrajectoryProfileCharts from './TrajectoryProfileCharts.vue'

const BaseChartStub = defineComponent({
  name: 'BaseChart',
  props: {
    option: { type: Object, required: true },
    height: { type: String, default: '300px' },
    label: { type: String, required: true },
  },
  template: '<div class="base-chart-stub" />',
})

const tracks: Track3D[] = [
  {
    id: 'actual',
    snapshotId: 'flight-1',
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
        groundSpeedMps: 200,
        segment: 'actual_future',
      },
    ],
  },
]

describe('TrajectoryProfileCharts', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('uses one shared cursor for both profile lines and current markers', async () => {
    const store = useTimeCursorStore()
    store.setTracks(tracks)
    store.setTime(15)
    const wrapper = mount(TrajectoryProfileCharts, {
      props: { tracks },
      global: { stubs: { BaseChart: BaseChartStub } },
    })
    await nextTick()

    const charts = wrapper.findAllComponents(BaseChartStub)
    expect(charts).toHaveLength(2)
    const altitude = charts[0]!.props('option') as {
      series: Array<{
        data: Array<[number, number]>
        markLine?: { data: Array<{ xAxis: number }> }
      }>
    }
    const speed = charts[1]!.props('option') as typeof altitude
    expect(altitude.series[0]!.markLine!.data[0]!.xAxis).toBe(15)
    expect(speed.series[0]!.markLine!.data[0]!.xAxis).toBe(15)
    expect(altitude.series[1]!.data).toEqual([[15, 1_500]])
    expect(speed.series[1]!.data).toEqual([[15, 150]])
  })

  it('degrades independently when a metric is unavailable', () => {
    const withoutSpeed = tracks.map((track) => ({
      ...track,
      points: track.points.map((point) => ({ ...point, groundSpeedMps: null })),
    }))
    useTimeCursorStore().setTracks(withoutSpeed)
    const wrapper = mount(TrajectoryProfileCharts, {
      props: { tracks: withoutSpeed },
      global: { stubs: { BaseChart: BaseChartStub } },
    })

    expect(wrapper.findAllComponents(BaseChartStub)).toHaveLength(1)
    expect(wrapper.get('[data-testid="ground-speed-empty"]').text()).toContain('未提供速度')
  })
})
