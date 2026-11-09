import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listBatches, searchFlights } from '@/api/client'
import FlightDetailEntryView from './FlightDetailEntryView.vue'
import FlightSearchView from './FlightSearchView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>()
  return { ...original, listBatches: vi.fn(), searchFlights: vi.fn() }
})

const batch = {
  batch: 'scat20161015_20161021',
  flight_count: 13138,
  earliest_iobt_utc: '2016-10-15T00:00:00Z',
  latest_iobt_utc: '2016-10-21T23:59:59Z',
}

const flight = {
  dataset_id: 42,
  source: {
    system: 'scat-public-dataset',
    batch: batch.batch,
    external_id: '100016',
    external_id_type: 'source-id',
  },
  identity: {
    callsign: 'SAS406',
    aircraft_type: 'B738',
    departure_airport: 'EKCH',
    destination_airport: 'ESSA',
    iobt_utc: '2016-10-20T18:00:00Z',
    atd_utc: '2016-10-20T18:10:00Z',
    candidate_key: 'candidate',
    identity_confidence: 1,
  },
  control_centre_transfer_count: 3,
  control_centre_history: [],
  ingest: { original_filename: null, uploaded_at: null },
}

function createTestRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/flights', name: 'flights', component: FlightSearchView },
      {
        path: '/flights/:batch/:externalId',
        name: 'flight-detail',
        component: FlightDetailEntryView,
      },
    ],
  })
}

describe('FlightSearchView', () => {
  beforeEach(() => {
    vi.mocked(listBatches).mockResolvedValue({
      pagination: { offset: 0, limit: 100, returned: 1, total: 1, has_more: false },
      items: [batch],
    })
    vi.mocked(searchFlights).mockResolvedValue({
      pagination: { offset: 20, limit: 20, returned: 1, total: 41, has_more: true },
      items: [flight],
    })
  })

  it('restores URL filters, requests server pagination, and keeps state on detail entry', async () => {
    const router = createTestRouter()
    await router.push(
      `/flights?batch=${batch.batch}&callsign=SAS406&sort_by=callsign&sort_order=desc&page_size=20&page=2`,
    )
    await router.isReady()

    const wrapper = mount(FlightSearchView, {
      global: { plugins: [createPinia(), router, ElementPlus] },
    })
    await flushPromises()

    expect(searchFlights).toHaveBeenCalledWith(
      expect.objectContaining({
        batch: batch.batch,
        callsign: 'SAS406',
        sort_by: 'callsign',
        sort_order: 'desc',
        limit: 20,
        offset: 20,
      }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(wrapper.text()).toContain('SAS406')
    expect(wrapper.text()).toContain('EKCH')

    await wrapper.find('tbody tr').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('flight-detail')
    expect(router.currentRoute.value.query.page).toBe('2')
    expect(router.currentRoute.value.params.externalId).toBe('100016')
  })

  it('validates the UTC time interval before querying', async () => {
    const router = createTestRouter()
    await router.push(`/flights?batch=${batch.batch}`)
    await router.isReady()
    const wrapper = mount(FlightSearchView, {
      global: { plugins: [createPinia(), router, ElementPlus] },
    })
    await flushPromises()
    vi.mocked(searchFlights).mockClear()

    const inputs = wrapper.findAll('input[type="datetime-local"]')
    await inputs[0].setValue('2016-10-21T00:00')
    await inputs[1].setValue('2016-10-20T00:00')
    await wrapper.find('form').trigger('submit')

    expect(wrapper.text()).toContain('开始时间必须早于结束时间')
    expect(searchFlights).not.toHaveBeenCalled()
  })

  it('renders a bounded empty result as a normal state', async () => {
    vi.mocked(searchFlights).mockResolvedValue({
      pagination: { offset: 0, limit: 20, returned: 0, total: 0, has_more: false },
      items: [],
    })
    const router = createTestRouter()
    await router.push(`/flights?batch=${batch.batch}`)
    await router.isReady()
    const wrapper = mount(FlightSearchView, {
      global: { plugins: [createPinia(), router, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('没有找到符合当前条件的航班')
  })
})
