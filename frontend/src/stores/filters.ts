import { defineStore } from 'pinia'

export const filterKeys = [
  'batch',
  'callsign',
  'aircraft_type',
  'adep',
  'ades',
  'candidate_key',
] as const

export type FilterKey = (typeof filterKeys)[number]
export type FilterState = Record<FilterKey, string>

const initialState = (): FilterState => ({
  batch: '',
  callsign: '',
  aircraft_type: '',
  adep: '',
  ades: '',
  candidate_key: '',
})

function firstQueryValue(value: unknown) {
  if (Array.isArray(value)) return typeof value[0] === 'string' ? value[0] : ''
  return typeof value === 'string' ? value : ''
}

export const useFilterStore = defineStore('filters', {
  state: initialState,
  getters: {
    activeCount: (state) => filterKeys.filter((key) => Boolean(state[key])).length,
  },
  actions: {
    hydrate(query: Record<string, unknown>) {
      filterKeys.forEach((key) => {
        this[key] = firstQueryValue(query[key]).trim()
      })
    },
    reset() {
      this.$patch(initialState())
    },
    toQuery() {
      return Object.fromEntries(
        filterKeys.flatMap((key) => (this[key] ? [[key, this[key]]] : [])),
      ) as Partial<FilterState>
    },
  },
})
