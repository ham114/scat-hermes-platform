<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQueryRaw } from 'vue-router'

import {
  listBatches,
  searchFlights,
  type BatchSummary,
  type FlightPage,
  type FlightSearchQuery,
} from '@/api/client'
import AsyncStatePanel from '@/components/AsyncStatePanel.vue'

type LoadState = 'loading' | 'empty' | 'error' | 'success'
type SortBy = 'iobt' | 'callsign' | 'external_id'
type SortOrder = 'asc' | 'desc'

interface SearchForm {
  batch: string
  callsign: string
  aircraft_type: string
  adep: string
  ades: string
  start_time: string
  end_time: string
  sort_by: SortBy
  sort_order: SortOrder
  page_size: number
}

const route = useRoute()
const router = useRouter()
const state = ref<LoadState>('loading')
const error = ref<unknown>()
const formError = ref('')
const batches = ref<BatchSummary[]>([])
const pageData = ref<FlightPage>()
const ready = ref(false)
const copyStatus = ref('')
let batchesController: AbortController | undefined
let searchController: AbortController | undefined

const form = reactive<SearchForm>({
  batch: '',
  callsign: '',
  aircraft_type: '',
  adep: '',
  ades: '',
  start_time: '',
  end_time: '',
  sort_by: 'iobt',
  sort_order: 'asc',
  page_size: 20,
})

const numberFormat = new Intl.NumberFormat('zh-CN')

function queryText(value: unknown) {
  const first = Array.isArray(value) ? value[0] : value
  return typeof first === 'string' ? first : ''
}

function safePositiveInt(value: unknown, fallback: number) {
  const parsed = Number.parseInt(queryText(value), 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
}

function toLocalInput(value: unknown) {
  const text = queryText(value)
  return text ? text.replace(/Z$/, '').slice(0, 16) : ''
}

function toApiTime(value: string) {
  return value ? `${value.length === 16 ? `${value}:00` : value}Z` : undefined
}

function hydrateForm() {
  form.batch = queryText(route.query.batch)
  form.callsign = queryText(route.query.callsign)
  form.aircraft_type = queryText(route.query.aircraft_type)
  form.adep = queryText(route.query.adep)
  form.ades = queryText(route.query.ades)
  form.start_time = toLocalInput(route.query.start_time)
  form.end_time = toLocalInput(route.query.end_time)
  form.sort_by = ['callsign', 'external_id'].includes(queryText(route.query.sort_by))
    ? (queryText(route.query.sort_by) as SortBy)
    : 'iobt'
  form.sort_order = queryText(route.query.sort_order) === 'desc' ? 'desc' : 'asc'
  form.page_size = [20, 50, 100].includes(safePositiveInt(route.query.page_size, 20))
    ? safePositiveInt(route.query.page_size, 20)
    : 20
}

const currentPage = computed(() => safePositiveInt(route.query.page, 1))
const pagination = computed(() => pageData.value?.pagination)
const pageCount = computed(() =>
  Math.max(1, Math.ceil((pagination.value?.total ?? 0) / form.page_size)),
)
const rangeStart = computed(() => (pagination.value?.total ? pagination.value.offset + 1 : 0))
const rangeEnd = computed(() =>
  pagination.value ? pagination.value.offset + pagination.value.returned : 0,
)

function normalizedQuery(page = 1): LocationQueryRaw {
  const query: LocationQueryRaw = {
    batch: form.batch.trim() || undefined,
    callsign: form.callsign.trim().toUpperCase() || undefined,
    aircraft_type: form.aircraft_type.trim().toUpperCase() || undefined,
    adep: form.adep.trim().toUpperCase() || undefined,
    ades: form.ades.trim().toUpperCase() || undefined,
    start_time: toApiTime(form.start_time),
    end_time: toApiTime(form.end_time),
    sort_by: form.sort_by,
    sort_order: form.sort_order,
    page_size: String(form.page_size),
    page: String(page),
  }
  return Object.fromEntries(Object.entries(query).filter(([, value]) => value))
}

function validateForm() {
  formError.value = ''
  if (form.start_time && form.end_time && form.start_time >= form.end_time) {
    formError.value = '开始时间必须早于结束时间。'
    return false
  }
  return true
}

async function loadFlights() {
  if (!ready.value) return
  searchController?.abort()
  const controller = new AbortController()
  searchController = controller
  state.value = 'loading'
  error.value = undefined
  copyStatus.value = ''

  const limit = safePositiveInt(route.query.page_size, 20)
  const page = safePositiveInt(route.query.page, 1)
  const query: FlightSearchQuery = {
    batch: queryText(route.query.batch) || undefined,
    callsign: queryText(route.query.callsign) || undefined,
    aircraft_type: queryText(route.query.aircraft_type) || undefined,
    adep: queryText(route.query.adep) || undefined,
    ades: queryText(route.query.ades) || undefined,
    start_time: queryText(route.query.start_time) || undefined,
    end_time: queryText(route.query.end_time) || undefined,
    sort_by: (queryText(route.query.sort_by) || 'iobt') as SortBy,
    sort_order: (queryText(route.query.sort_order) || 'asc') as SortOrder,
    limit,
    offset: (page - 1) * limit,
  }

  try {
    pageData.value = await searchFlights(query, { signal: controller.signal })
    if (page > pageCount.value && pageData.value.pagination.total) {
      await router.replace({ query: normalizedQuery(pageCount.value) })
      return
    }
    state.value = pageData.value.items.length ? 'success' : 'empty'
  } catch (reason) {
    if (controller.signal.aborted || searchController !== controller) return
    error.value = reason
    state.value = 'error'
  }
}

async function initialize() {
  hydrateForm()
  batchesController?.abort()
  batchesController = new AbortController()
  try {
    const result = await listBatches(
      { limit: 100, offset: 0 },
      { signal: batchesController.signal },
    )
    batches.value = result.items
    ready.value = true
    if (!form.batch && result.items.length) {
      form.batch = result.items.at(-1)?.batch ?? ''
      await router.replace({ query: normalizedQuery(1) })
    } else {
      await loadFlights()
    }
  } catch (reason) {
    error.value = reason
    state.value = 'error'
  }
}

async function submitSearch() {
  if (!validateForm()) return
  const destination = router.resolve({ query: normalizedQuery(1) })
  if (destination.fullPath === route.fullPath) await loadFlights()
  else await router.replace({ query: normalizedQuery(1) })
}

async function resetSearch() {
  const batch = form.batch || batches.value.at(-1)?.batch || ''
  Object.assign(form, {
    batch,
    callsign: '',
    aircraft_type: '',
    adep: '',
    ades: '',
    start_time: '',
    end_time: '',
    sort_by: 'iobt',
    sort_order: 'asc',
    page_size: 20,
  })
  formError.value = ''
  const destination = router.resolve({ query: normalizedQuery(1) })
  if (destination.fullPath === route.fullPath) await loadFlights()
  else await router.replace({ query: normalizedQuery(1) })
}

async function goToPage(page: number) {
  if (page < 1 || page > pageCount.value || page === currentPage.value) return
  await router.replace({ query: normalizedQuery(page) })
}

async function copyFilterUrl() {
  try {
    await navigator.clipboard.writeText(window.location.href)
    copyStatus.value = '筛选链接已复制'
  } catch {
    copyStatus.value = '复制失败，请从地址栏复制'
  }
}

function retry() {
  if (ready.value) void loadFlights()
  else void initialize()
}

function openFlight(item: FlightPage['items'][number]) {
  const batch = item.source.batch
  const externalId = item.source.external_id
  if (!batch || !externalId) return
  void router.push({
    name: 'flight-detail',
    params: { batch, externalId },
    query: { ...route.query },
  })
}

function formatUtc(value?: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'UTC',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

watch(
  () => route.query,
  () => {
    hydrateForm()
    if (ready.value) void loadFlights()
  },
  { deep: true },
)

onMounted(() => void initialize())
onBeforeUnmount(() => {
  batchesController?.abort()
  searchController?.abort()
})
</script>

<template>
  <section class="page flight-search-page">
    <header class="flight-search-heading">
      <div>
        <span class="eyebrow">M3-V2 · Flight registry</span>
        <h1>航班检索</h1>
      </div>
      <div class="share-block">
        <span role="status">{{ copyStatus }}</span>
        <el-button @click="copyFilterUrl">复制筛选链接</el-button>
      </div>
    </header>

    <form class="panel flight-filter-panel" @submit.prevent="submitSearch">
      <div class="flight-filter-grid">
        <label class="wide-field">
          <span>数据批次</span>
          <el-select v-model="form.batch" filterable placeholder="全部批次">
            <el-option label="全部批次" value="" />
            <el-option
              v-for="batch in batches"
              :key="batch.batch"
              :label="`${batch.batch} · ${numberFormat.format(batch.flight_count)}`"
              :value="batch.batch"
            />
          </el-select>
        </label>
        <label>
          <span>航班呼号</span>
          <el-input v-model="form.callsign" maxlength="20" placeholder="例如 SAS406" />
        </label>
        <label>
          <span>机型</span>
          <el-input v-model="form.aircraft_type" maxlength="10" placeholder="例如 B738" />
        </label>
        <label>
          <span>起飞机场</span>
          <el-input v-model="form.adep" maxlength="4" placeholder="ICAO 代码" />
        </label>
        <label>
          <span>到达机场</span>
          <el-input v-model="form.ades" maxlength="4" placeholder="ICAO 代码" />
        </label>
        <label>
          <span>计划开始（UTC）</span>
          <input v-model="form.start_time" class="native-control" type="datetime-local" />
        </label>
        <label>
          <span>计划结束（UTC）</span>
          <input v-model="form.end_time" class="native-control" type="datetime-local" />
        </label>
        <label>
          <span>排序字段</span>
          <el-select v-model="form.sort_by">
            <el-option label="计划时间" value="iobt" />
            <el-option label="航班呼号" value="callsign" />
            <el-option label="来源编号" value="external_id" />
          </el-select>
        </label>
        <label>
          <span>排序方向</span>
          <el-select v-model="form.sort_order">
            <el-option label="升序" value="asc" />
            <el-option label="降序" value="desc" />
          </el-select>
        </label>
      </div>
      <div class="filter-command-row">
        <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
        <p v-else>筛选条件为精确匹配；时间范围采用左闭右开区间。</p>
        <div>
          <el-button @click="resetSearch">重置</el-button>
          <el-button type="primary" native-type="submit" :loading="state === 'loading'">
            查询航班
          </el-button>
        </div>
      </div>
    </form>

    <article class="panel flight-results-panel">
      <header class="panel-header result-header">
        <div>
          <span class="panel-kicker">DETERMINISTIC RESULTS</span>
          <h2>航班列表</h2>
        </div>
        <span class="result-count">
          {{ numberFormat.format(rangeStart) }}–{{ numberFormat.format(rangeEnd) }} / 共
          {{ numberFormat.format(pagination?.total ?? 0) }} 条
        </span>
      </header>

      <AsyncStatePanel
        v-if="state !== 'success'"
        class="flight-result-state"
        :state="state"
        :error="error"
        empty-description="没有找到符合当前条件的航班。"
        @retry="retry"
      />

      <template v-else>
        <div class="flight-table-scroll">
          <table class="flight-table">
            <thead>
              <tr>
                <th>呼号 / 来源编号</th>
                <th>机型</th>
                <th>航路</th>
                <th>计划时间（UTC）</th>
                <th>实际起飞（UTC）</th>
                <th>管制移交</th>
                <th><span class="sr-only">进入详情</span></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in pageData?.items"
                :key="`${item.source.batch}-${item.source.external_id}`"
                tabindex="0"
                @click="openFlight(item)"
                @keyup.enter="openFlight(item)"
              >
                <td>
                  <strong>{{ item.identity.callsign || '未知呼号' }}</strong>
                  <small>{{ item.source.external_id || '—' }}</small>
                </td>
                <td>
                  <span class="aircraft-code">{{ item.identity.aircraft_type || '—' }}</span>
                </td>
                <td>
                  <span class="route-code">
                    {{ item.identity.departure_airport || '—' }}
                    <i>→</i>
                    {{ item.identity.destination_airport || '—' }}
                  </span>
                </td>
                <td>{{ formatUtc(item.identity.iobt_utc) }}</td>
                <td>{{ formatUtc(item.identity.atd_utc) }}</td>
                <td>{{ numberFormat.format(item.control_centre_transfer_count) }} 次</td>
                <td><span class="row-action">分析 →</span></td>
              </tr>
            </tbody>
          </table>
        </div>

        <footer class="pagination-bar">
          <label>
            每页
            <el-select v-model="form.page_size" aria-label="每页条数" @change="submitSearch">
              <el-option label="20" :value="20" />
              <el-option label="50" :value="50" />
              <el-option label="100" :value="100" />
            </el-select>
          </label>
          <div>
            <el-button :disabled="currentPage <= 1" @click="goToPage(currentPage - 1)">
              上一页
            </el-button>
            <span>第 {{ currentPage }} / {{ pageCount }} 页</span>
            <el-button :disabled="currentPage >= pageCount" @click="goToPage(currentPage + 1)">
              下一页
            </el-button>
          </div>
        </footer>
      </template>
    </article>
  </section>
</template>
