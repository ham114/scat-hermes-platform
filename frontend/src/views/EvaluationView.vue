<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  listAnalysisArtifacts,
  listBatches,
  listMetricSummaries,
  listModelExperiments,
  listWorstPredictionSamples,
  type AnalysisArtifactSummary,
  type BatchSummary,
  type MetricGroupBy,
  type MetricSummaryPage,
  type ModelExperimentHorizonMetric,
  type ModelExperimentSummary,
  type WorstPredictionMetric,
  type WorstPredictionSamplePage,
} from '@/api/client'
import AsyncStatePanel from '@/components/AsyncStatePanel.vue'
import BaseChart from '@/components/BaseChart.vue'
import { useFilterStore } from '@/stores/filters'
import {
  downloadMetricSummaryCsv,
  downloadWorstSamplesCsv,
  metricGroupLabel,
} from '@/utils/evaluationExport'

type LoadState = 'loading' | 'empty' | 'error' | 'success'

const HORIZONS = [60, 180, 300, 600, 900, 1200] as const

const GROUP_OPTIONS: Array<{ value: MetricGroupBy; label: string }> = [
  { value: 'overall', label: '整批次' },
  { value: 'phase', label: '主导阶段' },
  { value: 'aircraft_type', label: '机型' },
]

const METRIC_OPTIONS: Array<{ value: WorstPredictionMetric; label: string }> = [
  { value: 'horizontal', label: '水平误差' },
  { value: 'vertical', label: '垂直误差' },
  { value: 'along', label: '沿航向' },
  { value: 'cross', label: '横向' },
]

const LIMIT_OPTIONS = [10, 20, 50]

const PHASE_LABELS: Record<string, string> = {
  takeoff_initial_climb: '起飞/初始爬升',
  climb: '爬升',
  high_cruise: '高空巡航',
  midlow_level: '中低空飞行',
  descent: '下降',
  approach: '进场',
  unknown: '未知',
}

const PREDICTOR_LABELS: Record<string, string> = {
  'kalman-cv': 'Kalman 常速度',
  'constant-velocity-heading': '匀速-恒航向',
}

const filters = useFilterStore()
const route = useRoute()
const batches = ref<BatchSummary[]>([])
const summaryState = ref<LoadState>('loading')
const summaryError = ref<unknown>()
const summary = ref<MetricSummaryPage>()
const groupBy = ref<MetricGroupBy>('overall')
const horizon = ref<number>(300)
const worstState = ref<LoadState>('loading')
const worstError = ref<unknown>()
const worst = ref<WorstPredictionSamplePage>()
const worstMetric = ref<WorstPredictionMetric>('horizontal')
const worstLimit = ref(10)
const experimentsState = ref<LoadState>('loading')
const experimentsError = ref<unknown>()
const experiments = ref<ModelExperimentSummary[]>([])
const artifactsState = ref<LoadState>('loading')
const artifactsError = ref<unknown>()
const comparisonArtifacts = ref<AnalysisArtifactSummary[]>([])
let batchesController: AbortController | undefined
let summaryController: AbortController | undefined
let worstController: AbortController | undefined
let experimentsController: AbortController | undefined
let artifactsController: AbortController | undefined

const numberFormat = new Intl.NumberFormat('zh-CN')
const latestComparisonArtifact = computed(() => comparisonArtifacts.value[0] ?? null)

const overallRows = computed(() =>
  (summary.value?.summaries ?? [])
    .filter((row) => row.group_by === 'overall')
    .sort((left, right) => left.horizon_s - right.horizon_s),
)

const tableRows = computed(() =>
  (summary.value?.summaries ?? [])
    .filter((row) => row.group_by === groupBy.value)
    .sort(
      (left, right) => left.horizon_s - right.horizon_s || right.sample_count - left.sample_count,
    ),
)

const selectedOverall = computed(() =>
  overallRows.value.find((row) => row.horizon_s === horizon.value),
)

const metricVersion = computed(() => summary.value?.summaries[0]?.metric_version ?? '—')

const modelColumns = computed(() => {
  const latest = new Map<string, ModelExperimentSummary>()
  for (const item of experiments.value) {
    if (item.protocol_version !== 'm6-eval-v1') continue
    if (item.status !== 'succeeded' && item.status !== 'partial') continue
    if (!(item.metrics ?? []).length) continue
    const existing = latest.get(item.predictor_id)
    if (!existing || item.created_at > existing.created_at) latest.set(item.predictor_id, item)
  }
  return [...latest.values()]
    .sort((left, right) => left.predictor_id.localeCompare(right.predictor_id))
    .map((item) => {
      const byHorizon = new Map<number, ModelExperimentHorizonMetric>()
      for (const metric of item.metrics ?? []) byHorizon.set(metric.horizon_s, metric)
      return {
        key: item.experiment_id,
        label: PREDICTOR_LABELS[item.predictor_id] ?? item.predictor_id,
        version: item.predictor_version,
        experimentId: item.experiment_id,
        finishedAt: item.finished_at,
        flightCount: item.dataset?.flight_count ?? null,
        byHorizon,
      }
    })
})

const cfplByHorizon = computed(() => {
  const map = new Map<number, { mae: number | null; samples: number | null }>()
  for (const row of overallRows.value) {
    map.set(row.horizon_s, { mae: row.horizontal_mae_m, samples: row.sample_count })
  }
  return map
})

function formatDistance(value: number | null | undefined) {
  if (value === null || value === undefined) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${value.toFixed(1)} m`
}

function formatCount(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : numberFormat.format(value)
}

function formatPhase(value: string | null | undefined) {
  if (!value) return '—'
  return PHASE_LABELS[value] ?? value
}

function formatMoment(value: string | null | undefined) {
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

function horizonLabel(value: number) {
  return value < 60 ? `${value} 秒` : `${Math.round(value / 60)} 分钟`
}

const metricCards = computed(() => {
  const row = selectedOverall.value
  if (!row) return []
  return [
    {
      label: '水平 MAE',
      value: formatDistance(row.horizontal_mae_m),
      unit: '',
      note: `${horizonLabel(row.horizon_s)} · 样本 ${formatCount(row.sample_count)}`,
    },
    {
      label: '水平中位数',
      value: formatDistance(row.horizontal_median_m),
      unit: '',
      note: `P90 ${formatDistance(row.horizontal_p90_m)}`,
    },
    {
      label: '水平 P95',
      value: formatDistance(row.horizontal_p95_m),
      unit: '',
      note: '95% 样本不超过该误差',
    },
    {
      label: '水平 RMSE',
      value: formatDistance(row.horizontal_rmse_m),
      unit: '',
      note: `沿航向 MAE ${formatDistance(row.along_track_mae_m)}`,
    },
    {
      label: '垂直 MAE',
      value: formatDistance(row.vertical_mae_m),
      unit: '',
      note: `中位数 ${formatDistance(row.vertical_median_m)}`,
    },
    {
      label: '覆盖航班',
      value: formatCount(row.flight_count),
      unit: '架',
      note: `本批次六档时域共 ${formatCount(summary.value?.record_count)} 条分组记录`,
    },
  ]
})

const chartText = '#8fa7ba'
const chartLine = 'rgba(100, 142, 173, 0.18)'

const trendOption = computed(() => ({
  animationDuration: 350,
  grid: { left: 64, right: 24, top: 28, bottom: 40 },
  tooltip: { trigger: 'axis', valueFormatter: (value: number) => formatDistance(value) },
  legend: { textStyle: { color: chartText }, top: 0 },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: overallRows.value.map((row) => horizonLabel(row.horizon_s)),
    axisLabel: { color: chartText },
    axisLine: { lineStyle: { color: chartLine } },
    axisTick: { show: false },
  },
  yAxis: {
    type: 'value',
    axisLabel: { color: chartText, formatter: (value: number) => `${Math.round(value / 1000)}k` },
    splitLine: { lineStyle: { color: chartLine } },
  },
  series: [
    {
      name: '水平 MAE',
      type: 'line',
      data: overallRows.value.map((row) => row.horizontal_mae_m),
      smooth: 0.25,
      showSymbol: true,
      lineStyle: { width: 2, color: '#39d0ff' },
    },
    {
      name: '水平中位数',
      type: 'line',
      data: overallRows.value.map((row) => row.horizontal_median_m),
      smooth: 0.25,
      showSymbol: false,
      lineStyle: { width: 2, color: '#37e0aa' },
    },
    {
      name: '水平 P95',
      type: 'line',
      data: overallRows.value.map((row) => row.horizontal_p95_m),
      smooth: 0.25,
      showSymbol: false,
      lineStyle: { width: 2, color: '#f5a524', type: 'dashed' },
    },
    {
      name: '垂直 MAE',
      type: 'line',
      data: overallRows.value.map((row) => row.vertical_mae_m),
      smooth: 0.25,
      showSymbol: false,
      lineStyle: { width: 2, color: '#a78bfa' },
    },
  ],
}))

function pickHorizon(value: unknown, fallback: number) {
  const parsed = Number(Array.isArray(value) ? value[0] : value)
  return (HORIZONS as readonly number[]).includes(parsed) ? parsed : fallback
}

async function loadBatches() {
  batchesController?.abort()
  batchesController = new AbortController()
  summaryState.value = 'loading'
  summaryError.value = undefined

  try {
    const result = await listBatches(
      { limit: 100, offset: 0 },
      { signal: batchesController.signal },
    )
    batches.value = result.items
    if (!result.items.length) {
      summaryState.value = 'empty'
      worstState.value = 'empty'
      return
    }
    if (!result.items.some((item) => item.batch === filters.batch)) {
      filters.batch = result.items.at(-1)?.batch ?? ''
    } else {
      await loadSummaries()
      await loadWorst()
      await loadExperiments()
      await loadComparisonArtifacts()
    }
  } catch (reason) {
    summaryError.value = reason
    summaryState.value = 'error'
  }
}

async function loadSummaries() {
  if (!filters.batch) return
  summaryController?.abort()
  summaryController = new AbortController()
  summaryState.value = 'loading'
  summaryError.value = undefined

  try {
    const result = await listMetricSummaries(
      filters.batch,
      { limit: 500 },
      { signal: summaryController.signal },
    )
    summary.value = result
    summaryState.value = result.summaries.length ? 'success' : 'empty'
  } catch (reason) {
    summaryError.value = reason
    summaryState.value = 'error'
  }
}

async function loadWorst() {
  if (!filters.batch) return
  worstController?.abort()
  worstController = new AbortController()
  worstState.value = 'loading'
  worstError.value = undefined

  try {
    const result = await listWorstPredictionSamples(
      filters.batch,
      { metric: worstMetric.value, horizon_s: horizon.value, limit: worstLimit.value },
      { signal: worstController.signal },
    )
    worst.value = result
    worstState.value = result.samples.length ? 'success' : 'empty'
  } catch (reason) {
    worstError.value = reason
    worstState.value = 'error'
  }
}

async function loadExperiments() {
  if (!filters.batch) return
  experimentsController?.abort()
  experimentsController = new AbortController()
  experimentsState.value = 'loading'
  experimentsError.value = undefined

  try {
    const result = await listModelExperiments(
      { batch: filters.batch, limit: 20 },
      { signal: experimentsController.signal },
    )
    experiments.value = result.experiments
    experimentsState.value = modelColumns.value.length ? 'success' : 'empty'
  } catch (reason) {
    experimentsError.value = reason
    experimentsState.value = 'error'
  }
}

async function loadComparisonArtifacts() {
  if (!filters.batch) return
  artifactsController?.abort()
  artifactsController = new AbortController()
  artifactsState.value = 'loading'
  artifactsError.value = undefined

  try {
    const result = await listAnalysisArtifacts(
      { operation: 'run_model_comparison', batch: filters.batch, limit: 5 },
      { signal: artifactsController.signal },
    )
    comparisonArtifacts.value = result.artifacts
    artifactsState.value = result.artifacts.length ? 'success' : 'empty'
  } catch (reason) {
    artifactsError.value = reason
    artifactsState.value = 'error'
  }
}

function exportSummaryCsv() {
  if (!summary.value || !tableRows.value.length) return
  downloadMetricSummaryCsv({ ...summary.value, summaries: tableRows.value })
}

function exportWorstCsv() {
  if (!worst.value || !worst.value.samples.length) return
  downloadWorstSamplesCsv(worst.value)
}

watch(
  () => filters.batch,
  () => {
    void loadSummaries()
    void loadWorst()
    void loadExperiments()
    void loadComparisonArtifacts()
  },
)

watch([worstMetric, worstLimit, horizon], () => {
  void loadWorst()
})

onMounted(() => {
  horizon.value = pickHorizon(route.query.horizon, horizon.value)
  void loadBatches()
})

onBeforeUnmount(() => {
  batchesController?.abort()
  summaryController?.abort()
  worstController?.abort()
  experimentsController?.abort()
  artifactsController?.abort()
})
</script>

<template>
  <section class="view evaluation-view">
    <article class="panel batch-control-panel">
      <div class="batch-select-block">
        <label for="evaluation-batch">SCAT 批次</label>
        <el-select
          id="evaluation-batch"
          :model-value="filters.batch"
          filterable
          placeholder="选择 SCAT 批次"
          :loading="summaryState === 'loading' && !batches.length"
          @update:model-value="(value: string) => (filters.batch = value)"
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
        <span>指标版本（口径 v2）</span>
        <strong>{{ metricVersion }}</strong>
      </div>
      <div class="dependency-state">
        <span :class="['status-dot', { warning: summaryState === 'error' }]"></span>
        <span>
          <small>确定性数据服务</small>
          <strong>{{
            summaryState === 'success' ? '在线' : summaryState === 'error' ? '异常' : '检查中'
          }}</strong>
        </span>
      </div>
    </article>

    <AsyncStatePanel
      v-if="summaryState !== 'success'"
      class="overview-state panel"
      :state="summaryState"
      :error="summaryError"
      empty-description="该批次还没有物化的预测指标汇总（需先跑 prediction-metrics 与 metric-summaries 作业）。"
      @retry="loadBatches"
    />

    <template v-else>
      <div class="business-metrics" aria-label="所选时域的核心指标">
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

      <article class="panel evaluation-trend-panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">HORIZON SWEEP · UTC SNAPSHOT</span>
            <h2>误差随时域变化</h2>
          </div>
          <div class="granularity-switch" aria-label="预测时域">
            <button
              v-for="value in HORIZONS"
              :key="value"
              :class="{ active: horizon === value }"
              @click="horizon = value"
            >
              {{ horizonLabel(value) }}
            </button>
          </div>
        </header>
        <BaseChart
          v-if="overallRows.length"
          :option="trendOption"
          label="水平与垂直误差随时域变化的折线图"
          height="320px"
        />
        <el-empty v-else description="暂无整体分组汇总" :image-size="64" />
      </article>

      <article class="panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">TRAJECTORY REPLAY</span>
            <h2>典型案例回放</h2>
          </div>
          <button class="ghost-button" @click="loadComparisonArtifacts">刷新</button>
        </header>
        <AsyncStatePanel
          v-if="artifactsState !== 'success'"
          :state="artifactsState"
          :error="artifactsError"
          empty-description="该批次还没有可回放的多模型对比结果，可先在智能分析中创建四方法对比任务。"
          @retry="loadComparisonArtifacts"
        />
        <div v-else-if="latestComparisonArtifact" class="replay-entry">
          <div>
            <strong>最近生成的四方法对比</strong>
            <p>
              航班 {{ latestComparisonArtifact.external_id ?? '多个' }} ·
              {{ latestComparisonArtifact.snapshot_count ?? 1 }} 个快照 ·
              {{ formatMoment(latestComparisonArtifact.created_at) }} UTC
            </p>
          </div>
          <router-link
            class="primary-button replay-link"
            :to="{
              name: 'visualization-3d',
              params: { artifactId: latestComparisonArtifact.artifact_id },
            }"
          >
            打开误差回放
          </router-link>
        </div>
      </article>

      <article class="panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">GROUPED METRICS</span>
            <h2>指标汇总</h2>
          </div>
          <div class="panel-actions">
            <el-select v-model="groupBy" size="small" style="width: 132px">
              <el-option
                v-for="option in GROUP_OPTIONS"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
            <button class="ghost-button" :disabled="!tableRows.length" @click="exportSummaryCsv">
              导出 CSV
            </button>
          </div>
        </header>
        <div class="evaluation-table-scroll">
          <table class="evaluation-table">
            <thead>
              <tr>
                <th>分组</th>
                <th>时域</th>
                <th>样本量</th>
                <th>航班数</th>
                <th>水平 MAE</th>
                <th>水平中位数</th>
                <th>水平 P95</th>
                <th>垂直 MAE</th>
                <th>垂直中位数</th>
                <th>垂直 P95</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in tableRows"
                :key="`${row.group_by}-${row.group_value}-${row.horizon_s}`"
                :class="{ highlight: row.group_by === 'overall' && row.horizon_s === horizon }"
              >
                <td>{{ metricGroupLabel(row) }}</td>
                <td>{{ horizonLabel(row.horizon_s) }}</td>
                <td>{{ formatCount(row.sample_count) }}</td>
                <td>{{ formatCount(row.flight_count) }}</td>
                <td>{{ formatDistance(row.horizontal_mae_m) }}</td>
                <td>{{ formatDistance(row.horizontal_median_m) }}</td>
                <td>{{ formatDistance(row.horizontal_p95_m) }}</td>
                <td>{{ formatDistance(row.vertical_mae_m) }}</td>
                <td>{{ formatDistance(row.vertical_median_m) }}</td>
                <td>{{ formatDistance(row.vertical_p95_m) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">WORST CASES</span>
            <h2>最差案例</h2>
          </div>
          <div class="panel-actions">
            <el-select v-model="worstMetric" size="small" style="width: 124px">
              <el-option
                v-for="option in METRIC_OPTIONS"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
            <el-select v-model="worstLimit" size="small" style="width: 96px">
              <el-option
                v-for="value in LIMIT_OPTIONS"
                :key="value"
                :label="`TOP ${value}`"
                :value="value"
              />
            </el-select>
            <button class="ghost-button" :disabled="!worst?.samples.length" @click="exportWorstCsv">
              导出 CSV
            </button>
          </div>
        </header>
        <AsyncStatePanel
          v-if="worstState !== 'success'"
          :state="worstState"
          :error="worstError"
          empty-description="该时域没有有效样本。"
          @retry="loadWorst"
        />
        <div v-else class="evaluation-table-scroll">
          <table class="evaluation-table">
            <thead>
              <tr>
                <th>航班</th>
                <th>快照时刻（UTC）</th>
                <th>时域</th>
                <th>水平误差</th>
                <th>垂直误差</th>
                <th>沿航向</th>
                <th>横向</th>
                <th>主导阶段</th>
                <th>机型</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="sample in worst?.samples"
                :key="`${sample.external_id}-${sample.snapshot_time}`"
              >
                <td>
                  <router-link
                    :to="{
                      name: 'flight-detail',
                      params: { batch: filters.batch, externalId: sample.external_id },
                    }"
                  >
                    {{ sample.external_id }}
                  </router-link>
                </td>
                <td>{{ formatMoment(sample.snapshot_time) }}</td>
                <td>{{ horizonLabel(sample.horizon_s) }}</td>
                <td>{{ formatDistance(sample.horizontal_error_m) }}</td>
                <td>{{ formatDistance(sample.vertical_error_m) }}</td>
                <td>{{ formatDistance(sample.along_track_error_m) }}</td>
                <td>{{ formatDistance(sample.cross_track_error_m) }}</td>
                <td>{{ formatPhase(sample.dominant_phase) }}</td>
                <td>{{ sample.aircraft_type ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="panel">
        <header class="panel-header">
          <div>
            <span class="panel-kicker">SELF-DEVELOPED MODELS · m6-eval-v1</span>
            <h2>自研模型对比</h2>
          </div>
          <div class="panel-actions">
            <button class="ghost-button" @click="loadExperiments">刷新</button>
          </div>
        </header>
        <p class="model-comparison-note">
          六档时域水平 MAE 对照（括注样本量）：CFPL 基准 = 空管计划航路 vs 实飞（v2 口径）；
          自研模型 = 从实飞观测外推（m6-eval-v1）。两组口径独立，仅作同页对照，不可混算或直接加减。
        </p>
        <AsyncStatePanel
          v-if="experimentsState !== 'success'"
          :state="experimentsState"
          :error="experimentsError"
          empty-description="该批次还没有可用的自研模型实验；可在对话中提交实验（prepare → 确认 → submit），或运行 scat.experiments.cli。"
          @retry="loadExperiments"
        />
        <template v-else>
          <div class="evaluation-table-scroll">
            <table class="evaluation-table">
              <thead>
                <tr>
                  <th>时域</th>
                  <th>
                    CFPL 基准（v2）
                    <small>计划 vs 实飞</small>
                  </th>
                  <th v-for="model in modelColumns" :key="model.key">
                    {{ model.label }}
                    <small>
                      v{{ model.version }} ·
                      {{ model.flightCount === null ? '—' : formatCount(model.flightCount) }} 架
                    </small>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="value in HORIZONS"
                  :key="value"
                  :class="{ highlight: value === horizon }"
                >
                  <td>{{ horizonLabel(value) }}</td>
                  <td>
                    {{ formatDistance(cfplByHorizon.get(value)?.mae) }}
                    <small>（{{ formatCount(cfplByHorizon.get(value)?.samples) }} 样本）</small>
                  </td>
                  <td v-for="model in modelColumns" :key="model.key">
                    {{ formatDistance(model.byHorizon.get(value)?.mae_m) }}
                    <small
                      >（{{ formatCount(model.byHorizon.get(value)?.sample_count) }} 样本）</small
                    >
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="model-experiment-meta">
            <span v-for="model in modelColumns" :key="model.key">
              {{ model.experimentId }} ·
              {{ model.finishedAt ? formatMoment(model.finishedAt) : '运行中' }}
            </span>
          </p>
        </template>
      </article>

      <footer class="overview-footnote">
        <span>对比对象：空管系统下发的飞行计划航路（CFPL）与实飞轨迹；非模型预测</span>
        <span>口径版本 {{ metricVersion }} · 所有时间按 UTC 展示</span>
      </footer>
    </template>
  </section>
</template>
