import ElementPlus from 'element-plus'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { Track3D } from '@/visualization/types'

import TrajectoryWorstErrorPanel from './TrajectoryWorstErrorPanel.vue'

const track: Track3D = {
  id: 'snapshot::kalman-cv',
  snapshotId: 'snapshot',
  label: 'Kalman 常速度',
  kind: 'predicted',
  color: '#8b5cf6',
  points: [
    {
      time: 10,
      longitude: 20,
      latitude: 60,
      altitudeMetres: 3_000,
      groundSpeedMps: 100,
      horizontalErrorMetres: 120,
      segment: 'predicted',
    },
    {
      time: 20,
      longitude: 20.1,
      latitude: 60.1,
      altitudeMetres: 3_100,
      groundSpeedMps: 110,
      horizontalErrorMetres: 2_450,
      segment: 'predicted',
    },
  ],
}

describe('TrajectoryWorstErrorPanel', () => {
  it('shows backend metrics and emits the selected shared time', async () => {
    const wrapper = mount(TrajectoryWorstErrorPanel, {
      props: {
        tracks: [track],
        metrics: [
          {
            methodId: 'kalman-cv',
            adeMetres: 1_285,
            fdeMetres: 2_450,
            maxErrorMetres: 2_450,
            worstTimeSeconds: 20,
          },
        ],
      },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).toContain('2.45 km')
    expect(wrapper.text()).toContain('1.28 km')
    expect(wrapper.text()).toContain('+00:20')
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('locate')?.[0]).toEqual([
      { trackId: 'snapshot::kalman-cv', time: 20, horizontalErrorMetres: 2_450 },
    ])
  })
})
