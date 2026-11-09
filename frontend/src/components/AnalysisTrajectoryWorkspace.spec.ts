import { mount, flushPromises } from '@vue/test-utils'
import { describe, it, expect } from 'vitest'
import AnalysisTrajectoryWorkspace from './AnalysisTrajectoryWorkspace.vue'
import Trajectory2dPlayback from './Trajectory2dPlayback.vue'
import TrajectoryPlaybackControls from './TrajectoryPlaybackControls.vue'
import type { TrajectoryBlock } from '@/lib/analysisDisplay'
const block: TrajectoryBlock = {
  type: 'trajectory_2d',
  title: '合成地理轨迹',
  x_key: 'lon',
  y_key: 'lat',
  x_label: '经度',
  y_label: '纬度',
  x_unit: 'degree',
  y_unit: 'degree',
  time_key: 't',
  series: [],
  points: [
    { lon: 116, lat: 39, t: 0 },
    { lon: 116.1, lat: 39.1, t: 10 },
  ],
}
describe('trajectory workspace parity', () => {
  it('keeps timeless geometry visible without pretending sequence indices are seconds', async () => {
    const wrapper = mount(AnalysisTrajectoryWorkspace, {
      props: { block: { ...block, time_key: null } },
      global: {
        stubs: {
          Trajectory3dView: {
            name: 'Trajectory3dView',
            props: ['tracks', 'dimension'],
            template: '<div />',
          },
        },
      },
    })
    await flushPromises()
    expect(wrapper.findComponent(TrajectoryPlaybackControls).exists()).toBe(false)
    expect(wrapper.text()).toContain('显示静态轨迹')
    const map = wrapper.findComponent({ name: 'Trajectory3dView' })
    expect(map.props('tracks')).toHaveLength(1)
    expect(map.props('tracks')[0].timeBasis).toBe('sequence')
    wrapper.unmount()
  })
  it('reuses playback, profiles and map components with independent cursors', async () => {
    const options = {
      props: { block },
      global: { stubs: { BaseChart: true, Trajectory3dView: true, 'el-button': false } },
    }
    const a = mount(AnalysisTrajectoryWorkspace, options),
      b = mount(AnalysisTrajectoryWorkspace, options)
    expect(a.findComponent(Trajectory2dPlayback).exists()).toBe(false)
    expect(a.findComponent({ name: 'Trajectory3dView' }).attributes('dimension')).toBe('2d')
    expect(a.findComponent(TrajectoryPlaybackControls).props('tracks')).toHaveLength(1)
    const storeA = (
      a.findComponent(TrajectoryPlaybackControls).vm as unknown as {
        store: { setTime: (n: number) => void; currentTime: number }
      }
    ).store
    const storeB = (
      b.findComponent(TrajectoryPlaybackControls).vm as unknown as {
        store: { currentTime: number }
      }
    ).store
    storeA.setTime(5)
    expect(storeB.currentTime).toBe(0)
    await a.get('button').trigger('click')
    await flushPromises()
    expect(a.findComponent(Trajectory2dPlayback).exists()).toBe(false)
    a.unmount()
    b.unmount()
  })
})
