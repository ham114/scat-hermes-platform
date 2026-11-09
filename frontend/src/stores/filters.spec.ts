import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useFilterStore } from './filters'

describe('filter store', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('hydrates supported filters and drops unrelated query keys', () => {
    const store = useFilterStore()
    store.hydrate({ batch: ' scat-a ', callsign: ['CCA123', 'ignored'], page: '2' })

    expect(store.batch).toBe('scat-a')
    expect(store.callsign).toBe('CCA123')
    expect(store.activeCount).toBe(2)
    expect(store.toQuery()).toEqual({ batch: 'scat-a', callsign: 'CCA123' })
  })

  it('resets all filters', () => {
    const store = useFilterStore()
    store.batch = 'scat-a'
    store.adep = 'ZBAA'
    store.reset()

    expect(store.activeCount).toBe(0)
    expect(store.toQuery()).toEqual({})
  })
})
