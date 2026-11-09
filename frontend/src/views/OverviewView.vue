<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { getBatchOverview, listBatches, type BatchOverview, type BatchSummary } from '@/api/client'
import AsyncStatePanel from '@/components/AsyncStatePanel.vue'
import BaseChart from '@/components/BaseChart.vue'
import { useFilterStore } from '@/stores/filters'
import { downloadOverviewCsv } from '@/utils/overviewExport'

type LoadState = 'loading' | 'empty' | 'error' | 'success'
type Granularity = 'day' | 'hour'

const route = useRoute()
const router = useRouter()
const filters = useFilterStore()
const state = ref<LoadState>('loading')
const error = ref<unknown>()
const batches = ref<BatchSummary[]>([])
const overview = ref<BatchOverview>()
const granularity = ref<Granularity>(route.query.granularity === 'hour' ? 'hour' : 'day')
let batchesController: AbortController | undefined
let overviewController: AbortController | undefined

const numberFormat = new Intl.NumberFormat('zh-CN')
const percentFormat = new Intl.NumberFormat('zh-CN', {
  style: 'percent',
  maximumFractionDigits: 1,
})

const selectedBatch = computed(() => batches.value.find((item) => item.batch === filters.batch))

const stats = computed(() => overview.value?.statistics)
const predictionCoverage = computed(() => {
  if (!stats.value?.flight_count) return 0
  return stats.value.flights_with_prediction / stats.value.flight_count
})

function formatDate(value?: string | null) {
  if (!value) return '未知'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'UTC',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

const metricCards = computed(() => {
  if (!stats.value) return []
  return [
    {
      label: '航班总量',
      value: numberFormat.format(stats.value.flight_count),
      unit: '架次',
      note: `${numberFormat.format(stats.value.unique_callsign_count)} 个唯一呼号`,
    },
    {
      label: '轨迹点总量',
      value: numberFormat.format(stats.value.total_track_points),
      unit: '点',
      note: `平均 ${numberFormat.format(Math.round(stats.value.average_track_points))} 点/航班`,
    },
    {
      label: 'SCAT 预测覆盖',
      value: percentFormat.format(predictionCoverage.value),
      unit: '',
      note: `${numberFormat.format(stats.value.flights_with_prediction)} 架含预测`,
    },
    {
      label: '预测快照',
      value: numberFormat.format(stats.value.total_prediction_snapshots),
      unit: '份',
      note: `最多 ${numberFormat.format(stats.value.maximum_track_points)} 个轨迹点`,
    },
  ]
})

const chartText = '#8fa7ba'
const chartLine = 'rgba(100, 142, 173, 0.18)'

const volumeOption = computed(() => ({
  animationDuration: 350,
  grid: { left: 52, right: 20, top: 24, bottom: 42 },
  tooltip: { trigger: 'axis' },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: (overview.value?.flight_volume ?? []).map((item) => {
      const date = new Date(item.period_start)
      return granularity.value === 'hour'
        ? `${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')} ${String(date.getUTCHours()).padStart(2, '0')}:00`
        : `${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')}`
    }),
    axisLabel: { color: chartText, hideOverlap: true },
    axisLine: { lineStyle: { color: chartLine } },
    axisTick: { show: false },
  },
  yAxis: {
    type: 'value',
    minInterval: 1,
    axisLabel: { color: chartText },
    splitLine: { lineStyle: { color: chartLine } },
  },
  series: [
    {
      name: '航班量',
      type: 'line',
      data: (overview.value?.flight_volume ?? []).map((item) => item.flight_count),
      smooth: 0.25,
      showSymbol: false,
      lineStyle: { width: 2, color: '#39d0ff' },
      areaStyle: { color: 'rgba(57, 208, 255, 0.12)' },
    },
  ],
}))

function horizontalBarOption(labels: string[], values: number[], color: string) {
  return {
    animationDuration: 350,
    grid: { left: 70, right: 26, top: 10, bottom: 24 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: chartText },
      splitLine: { lineStyle: { color: chartLine } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: labels,
      axisLabel: { color: chartText, width: 62, overflow: 'truncate' },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: [
      {
        type: 'bar',
        data: values,
        barMaxWidth: 18,
        itemStyle: { color, borderRadius: [0, 2, 2, 0] },
      },
    ],
  }
}

const aircraftOption = computed(() => {
  const items = overview.value?.top_aircraft_types ?? []
  return horizontalBarOption(
    items.map((item) => item.aircraft_type),
    items.map((item) => item.flight_count),
    '#39d0ff',
  )
})

const routeOption = computed(() => {
  const items = overview.value?.top_routes ?? []
  return horizontalBarOption(
    items.map((item) => `${item.adep} → ${item.ades}`),
    items.map((item) => item.flight_count),
    '#37e0aa',
  )
})

const airportOption = computed(() => {
  const departures = overview.value?.top_departure_airports ?? []
  const arrivals = overview.value?.top_arrival_airports ?? []
  const labels = [...new Set([...departures, ...arrivals].map((item) => item.airport))]
  const departureCounts = new Map(departures.map((item) => [item.airport, item.flight_count]))
  const arrivalCounts = new Map(arrivals.map((item) => [item.airport, item.flight_count]))

  return {
    animationDuration: 350,
    grid: { left: 54, right: 26, top: 16, bottom: 34 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: { color: chartText, hideOverlap: true },
      axisLine: { lineStyle: { color: chartLine } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: chartText },
      splitLine: { lineStyle: { color: chartLine } },
    },
    series: [
      {
        name: '起飞航班',
        type: 'bar',
        data: labels.map((label) => departureCounts.get(label) ?? 0),
        barMaxWidth: 18,
        itemStyle: { color: '#39d0ff', borderRadius: [2, 2, 0, 0] },
      },
      {
        name: '到达航班',
        type: 'bar',
        data: labels.map((label) => arrivalCounts.get(label) ?? 0),
        barMaxWidth: 18,
        itemStyle: { color: '#37e0aa', borderRadius: [2, 2, 0, 0] },
      },
    ],
  }
})

const qualityItems = computed(() => {
  const quality = overview.value?.data_quality
  const total = stats.value?.flight_count ?? 0
  if (!quality || !total) return []

  const item = (label: string, missing: number, detail: string) => ({
    label,
    missing,
    detail,
    coverage: Math.max(0, (total - missing) / total),
  })

  return [
    item('航班呼号', quality.missing_callsign_count, '身份字段'),
    item('机型代码', quality.missing_aircraft_type_count, '身份字段'),
    item('起飞机场', quality.missing_departure_airport_count, '航路字段'),
    item('降落机场', quality.missing_arrival_airport_count, '航路字段'),
    item('计划时间', quality.missing_iobt_count, 'IOBT'),
    item('实际轨迹', quality.empty_trajectory_count, '非空轨迹'),
    item('SCAT 预测', quality.flights_without_prediction_count, '预测覆盖'),
  ]
})

async function loadBatches() {
  batchesController?.abort()
  batchesController = new AbortController()
  state.value = 'loading'
  error.value = undefined

  try {
    const result = await listBatches(
      { limit: 100, offset: 0 },
      { signal: batchesController.signal },
    )
    batches.value = result.items
    if (!result.items.length) {
      state.value = 'empty'
      return
    }
    if (!result.items.some((item) => item.batch === filters.batch)) {
      filters.batch = result.items.at(-1)?.batch ?? ''
    } else {
      await loadOverview()
    }
  } catch (reason) {
    error.value = reason
    state.value = 'error'
  }
}

async function loadOverview() {
  if (!filters.batch) return
  overviewController?.abort()
  overviewController = new AbortController()
  state.value = 'loading'
  error.value = undefined

  try {
    overview.value = await getBatchOverview(filters.batch, 10, granularity.value, {
      signal: overviewController.signal,
    })
    state.value = 'success'
  } catch (reason) {
    error.value = reason
    state.value = 'error'
  }
}

function setGranularity(value: Granularity) {
  if (granularity.value === value) return
  granularity.value = value
  void router.replace({ query: { ...route.query, granularity: value } })
}

function exportCsv() {
  if (selectedBatch.value && overview.value) {
    downloadOverviewCsv(selectedBatch.value, overview.value)
  }
}

watch(
  () => route.query.granularity,
  (value) => {
    const next = value === 'hour' ? 'hour' : 'day'
    if (granularity.value !== next) granularity.value = next
  },
)

watch([() => filters.batch, granularity], () => void loadOverview())

onMounted(() => void loadBatches())
onBeforeUnmount(() => {
  batchesController?.abort()
  overviewController?.abort()
})
</script>

<template>
  <section class="page overview-page">
    <header class="overview-toolbar">
      <div>
        <span class="eyebrow">M3-V1 · Operational overview</span>
        <h1>数据总览</h1>
      </div>
      <div class="overview-actions">
        <el-button :disabled="state !== 'success'" @click="exportCsv">导出 CSV</el-button>
        <el-button type="primary" :loading="state === 'loading'" @click="loadOverview">
          刷新数据
        </el-button>
      </div>
    </header>

    <article class="panel batch-control-panel">
      <div class="batch-select-block">
        <label for="batch-selector">数据批次</label>
        <el-select
          id="batch-selector"
          v-model="filters.batch"
          filterable
          placeholder="选择 SCAT 批次"
          :loading="state === 'loading' && !batches.length"
        >
          <el-option
            v-for="batch in batches"
            :key="batch.batch"
            :label="`${batch.batch} · ${numberFormat.format(batch.flight_count)} 架次`"
            :value="batch.batch"
          />
        </el-select>
      </div>
      <div class="batch-context">
        <span>计划时间范围（UTC）</span>
        <strong>
          {{ formatDate(selectedBatch?.earliest_iobt_utc) }}
          <i>→</i>
          {{ formatDate(selectedBatch?.latest_iobt_utc) }}
        </strong>
      </div>
      <div class="dependency-state">
        <span :class="['status-dot', { warning: state === 'error' }]"></span>
        <span>
          <small>确定性数据服务</small>
          <strong>{{
            state === 'success' ? '在线' : state === 'error' ? '异常' : '检查中'
          }}</strong>
        </span>
      </div>
    </article>

    <AsyncStatePanel
      v-if="state !== 'success'"
      class="overview-state panel"
      :state="state"
      :error="error"
      empty-description="当前没有可用的 SCAT 数据批次。"
      @retry="loadBatches"
    />

    <template v-else>
      <div class="business-metrics" aria-label="批次核心指标">
        <article v-for="(metric, index) in metricCards" :key="metric.label" class="business-metric">
          <span class="metric-index">0{{ index + 1 }}</span>
          <span class="metric-label">{{ metric.label }}</span>
          <div>
            <strong>{{ metric.value }}</strong
            ><small>{{ metric.unit }}</small>
          </div>
          <p>{{ metric.note }}</p>
        </article>
      </div>

      <article class="panel volume-panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">FLIGHT VOLUME · UTC</span>
            <h2>计划航班量</h2>
          </div>
          <div class="granularity-switch" aria-label="时间粒度">
            <button :class="{ active: granularity === 'day' }" @click="setGranularity('day')">
              按日
            </button>
            <button :class="{ active: granularity === 'hour' }" @click="setGranularity('hour')">
              按小时
            </button>
          </div>
        </header>
        <BaseChart
          v-if="overview?.flight_volume.length"
          :option="volumeOption"
          label="计划航班量时间趋势图"
          height="310px"
        />
        <el-empty v-else description="该批次没有可用的计划时间" :image-size="64" />
      </article>

      <div class="distribution-grid">
        <article class="panel chart-panel">
          <header class="panel-header">
            <div>
              <span class="panel-kicker">AIRCRAFT MIX</span>
              <h2>主要机型</h2>
            </div>
            <span class="panel-meta">TOP {{ overview?.top_aircraft_types.length }}</span>
          </header>
          <BaseChart
            v-if="overview?.top_aircraft_types.length"
            :option="aircraftOption"
            label="主要机型航班量条形图"
          />
          <el-empty v-else description="暂无机型分布" :image-size="64" />
        </article>

        <article class="panel chart-panel">
          <header class="panel-header">
            <div>
              <span class="panel-kicker">ROUTE NETWORK</span>
              <h2>热门航线</h2>
            </div>
            <span class="panel-meta">TOP {{ overview?.top_routes.length }}</span>
          </header>
          <BaseChart
            v-if="overview?.top_routes.length"
            :option="routeOption"
            label="热门航线航班量条形图"
          />
          <el-empty v-else description="暂无航线分布" :image-size="64" />
        </article>

        <article class="panel chart-panel airport-panel">
          <header class="panel-header">
            <div>
              <span class="panel-kicker">AIRPORT ACTIVITY</span>
              <h2>机场热点</h2>
            </div>
            <div class="chart-legend" aria-label="机场图例">
              <span><i class="departure"></i>起飞</span>
              <span><i class="arrival"></i>到达</span>
            </div>
          </header>
          <BaseChart
            v-if="overview?.top_departure_airports.length || overview?.top_arrival_airports.length"
            :option="airportOption"
            label="主要机场起飞和到达航班量条形图"
            height="330px"
          />
          <el-empty v-else description="暂无机场分布" :image-size="64" />
        </article>
      </div>

      <article class="panel quality-panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">DATA COMPLETENESS</span>
            <h2>数据质量摘要</h2>
          </div>
          <span class="panel-meta"
            >基于 {{ numberFormat.format(stats?.flight_count ?? 0) }} 架航班</span
          >
        </header>
        <div class="quality-grid">
          <div v-for="item in qualityItems" :key="item.label" class="quality-item">
            <div class="quality-heading">
              <span
                ><strong>{{ item.label }}</strong
                ><small>{{ item.detail }}</small></span
              >
              <b>{{ percentFormat.format(item.coverage) }}</b>
            </div>
            <div class="quality-track">
              <i :style="{ width: `${item.coverage * 100}%` }"></i>
            </div>
            <p>{{ numberFormat.format(item.missing) }} 架缺失</p>
          </div>
        </div>
      </article>

      <footer class="overview-footnote">
        <span>统计口径：SCAT API v1 确定性聚合</span>
        <span>所有时间均按 UTC 展示</span>
      </footer>
    </template>
  </section>
</template>
