import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getBatchOverview, listBatches } from '@/api/client'
import { useFilterStore } from '@/stores/filters'
import OverviewView from './OverviewView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>()
  return { ...original, listBatches: vi.fn(), getBatchOverview: vi.fn() }
})

const batch = {
  batch: 'scat20161015_20161021',
  flight_count: 2,
  earliest_iobt_utc: '2016-10-15T00:00:00Z',
  latest_iobt_utc: '2016-10-21T23:00:00Z',
}

const overview = {
  statistics: {
    batch: batch.batch,
    flight_count: 2,
    unique_callsign_count: 2,
    aircraft_type_count: 1,
    departure_airport_count: 1,
    arrival_airport_count: 1,
    total_track_points: 1000,
    average_track_points: 500,
    minimum_track_points: 480,
    maximum_track_points: 520,
    flights_with_prediction: 1,
    total_prediction_snapshots: 12,
  },
  top_aircraft_types: [{ aircraft_type: 'A320', flight_count: 2 }],
  top_routes: [{ adep: 'EKCH', ades: 'ESSA', flight_count: 2 }],
  top_departure_airports: [{ airport: 'EKCH', flight_count: 2 }],
  top_arrival_airports: [{ airport: 'ESSA', flight_count: 2 }],
  flight_volume: [{ period_start: '2016-10-20T00:00:00Z', flight_count: 2 }],
  statistics_source: 'live' as const,
  data_quality: {
    missing_callsign_count: 0,
    missing_aircraft_type_count: 0,
    missing_departure_airport_count: 0,
    missing_arrival_airport_count: 0,
    missing_iobt_count: 0,
    empty_trajectory_count: 0,
    flights_without_prediction_count: 1,
  },
}

describe('OverviewView', () => {
  beforeEach(() => {
    vi.mocked(listBatches).mockResolvedValue({
      pagination: { offset: 0, limit: 100, returned: 1, total: 1, has_more: false },
      items: [batch],
    })
    vi.mocked(getBatchOverview).mockResolvedValue(overview)
  })

  it('loads the latest batch and renders deterministic metrics', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/overview', component: OverviewView }],
    })
    await router.push('/overview')
    await router.isReady()
    const pinia = createPinia()
    useFilterStore(pinia).batch = batch.batch

    const wrapper = mount(OverviewView, {
      global: {
        plugins: [pinia, router, ElementPlus],
        stubs: { BaseChart: true },
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('数据总览')
    expect(wrapper.text()).toContain('1,000')
    expect(wrapper.text()).toContain('50%')
    expect(wrapper.text()).toContain('数据质量摘要')
    expect(wrapper.text()).toContain('机场热点')
    expect(getBatchOverview).toHaveBeenCalledWith(
      batch.batch,
      10,
      'day',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })
})
