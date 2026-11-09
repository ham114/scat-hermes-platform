import { watch } from 'vue'
import { useRoute, useRouter, type LocationQueryRaw } from 'vue-router'

import { filterKeys, useFilterStore } from '@/stores/filters'

function stableFilterQuery(query: Record<string, unknown>) {
  return filterKeys
    .map((key) => {
      const raw = query[key]
      const value = Array.isArray(raw) ? raw[0] : raw
      return `${key}=${typeof value === 'string' ? value : ''}`
    })
    .join('&')
}

export function useFilterQuerySync() {
  const route = useRoute()
  const router = useRouter()
  const filters = useFilterStore()

  watch(
    () => route.query,
    (query) => {
      if (stableFilterQuery(query) !== stableFilterQuery(filters.toQuery())) {
        filters.hydrate(query)
      }
    },
    { immediate: true },
  )

  watch(
    () => filters.toQuery(),
    (filterQuery) => {
      if (stableFilterQuery(route.query) === stableFilterQuery(filterQuery)) return

      const nextQuery: LocationQueryRaw = { ...route.query }
      filterKeys.forEach((key) => delete nextQuery[key])
      Object.assign(nextQuery, filterQuery)
      void router.replace({ query: nextQuery })
    },
    { deep: true },
  )
}
