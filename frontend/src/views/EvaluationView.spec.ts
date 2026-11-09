import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  listAnalysisArtifacts,
  listBatches,
  listMetricSummaries,
  listModelExperiments,
  listWorstPredictionSamples,
  type AnalysisArtifactSummaryPage,
  type MetricSummaryPage,
  type MetricSummaryRow,
  type ModelExperimentPage,
  type WorstPredictionSamplePage,
} from '@/api/client'
import { useFilterStore } from '@/stores/filters'
import EvaluationView from './EvaluationView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>()
  return {
    ...original,
    listAnalysisArtifacts: vi.fn(),
    listBatches: vi.fn(),
    listMetricSummaries: vi.fn(),
    listModelExperiments: vi.fn(),
    listWorstPredictionSamples: vi.fn(),
  }
})

const BATCH = 'scat20161015_20161021'

const batch = {
  batch: BATCH,
  flight_count: 10617,
  earliest_iobt_utc: '2016-10-15T00:00:00Z',
  latest_iobt_utc: '2016-10-21T23:00:00Z',
}

function summaryRow(overrides: Partial<MetricSummaryRow> = {}): MetricSummaryRow {
  return {
    batch: BATCH,
    horizon_s: 300,
    group_by: 'overall',
    group_value: 'all',
    sample_count: 411614,
    flight_count: 10597,
    horizontal_mae_m: 4690.3,
    horizontal_rmse_m: 8123.4,
    vertical_mae_m: 444.4,
    vertical_rmse_m: 601.2,
    along_track_mae_m: 1234.5,
    cross_track_mae_m: 3456.7,
    horizontal_median_m: 3268.6,
    horizontal_p90_m: 11500.0,
    horizontal_p95_m: 13815.6,
    vertical_median_m: 210.0,
    vertical_p90_m: 900.0,
    vertical_p95_m: 1200.0,
    percentile_method: 'approximate',
    metric_version: 'scat-prediction-metrics-v2',
    algorithm_version: '2',
    source_version: 'scat-import-2',
    generated_at: '2026-09-19T19:20:44Z',
    ...overrides,
  }
}

const summaries: MetricSummaryPage = {
  batch: BATCH,
  group_by: null,
  horizon_s: null,
  record_count: 3,
  summaries: [
    summaryRow({ horizon_s: 60, horizontal_mae_m: 2382.3, horizontal_median_m: 1382.2 }),
    summaryRow(),
    summaryRow({
      group_by: 'phase',
      group_value: 'high_cruise',
      horizontal_mae_m: 5120.0,
      vertical_mae_m: null,
    }),
  ],
}

const worst: WorstPredictionSamplePage = {
  batch: BATCH,
  metric: 'horizontal_error_m',
  horizon_s: 300,
  sample_count: 1,
  samples: [
    {
      batch: BATCH,
      external_id: '108567',
      snapshot_time: '2016-10-20T06:37:02.553000Z',
      horizon_s: 300,
      horizontal_error_m: 176906.4,
      vertical_error_m: -7155.0,
      along_track_error_m: 842.2,
      cross_track_error_m: -9764.8,
      is_valid: true,
      dominant_phase: 'high_cruise',
      aircraft_type: 'A388',
    },
  ],
}

const modelExperiments: ModelExperimentPage = {
  batch: BATCH,
  predictor_id: null,
  count: 3,
  experiments: [
    {
      experiment_id: 'm6eval-kalman-cv-scat20161015_20161021-20260920T075645-acce1d',
      kind: 'predictor_evaluation',
      status: 'succeeded',
      batch: BATCH,
      predictor_id: 'kalman-cv',
      predictor_version: '1',
      data_version: 'scat-import-2',
      protocol_version: 'm6-eval-v1',
      source: 'chat',
      created_at: '2026-09-20T07:56:45Z',
      finished_at: '2026-09-20T09:00:58Z',
      dataset: { flight_count: 200 },
      metrics: [
        {
          horizon_s: 60,
          sample_count: 2052,
          flight_count: 200,
          mae_m: 463.3,
          rmse_m: 1106.1,
          median_m: 110.4,
          p90_m: 1260.9,
          p95_m: 2365.0,
        },
        {
          horizon_s: 300,
          sample_count: 1793,
          flight_count: 200,
          mae_m: 5648.6,
          rmse_m: 10936.5,
          median_m: 1720.5,
          p90_m: 17019.9,
          p95_m: 26910.0,
        },
      ],
    },
    {
      experiment_id:
        'm6eval-constant-velocity-heading-scat20161015_20161021-20260920T073046-f21ee5',
      kind: 'predictor_evaluation',
      status: 'succeeded',
      batch: BATCH,
      predictor_id: 'constant-velocity-heading',
      predictor_version: '1',
      data_version: 'scat-import-2',
      protocol_version: 'm6-eval-v1',
      source: 'cli',
      created_at: '2026-09-20T07:30:46Z',
      finished_at: '2026-09-20T07:31:10Z',
      dataset: { flight_count: 5 },
      metrics: [
        {
          horizon_s: 60,
          sample_count: 56,
          flight_count: 5,
          mae_m: 1301.4,
          rmse_m: 2192.3,
          median_m: 896.4,
          p90_m: 3240.3,
          p95_m: 3800.6,
        },
      ],
    },
    {
      experiment_id: 'm6eval-ghost-model-scat20161015_20161021-20260920T010000-000000',
      kind: 'predictor_evaluation',
      status: 'pending',
      batch: BATCH,
      predictor_id: 'ghost-model',
      predictor_version: '1',
      data_version: 'scat-import-2',
      protocol_version: 'm6-eval-v1',
      source: 'chat',
      created_at: '2026-09-20T01:00:00Z',
      finished_at: null,
      metrics: null,
    },
  ],
}

const comparisonArtifacts: AnalysisArtifactSummaryPage = {
  count: 1,
  artifacts: [
    {
      artifact_id:
        'model_comparison-scat-comparison-v1-scat-scat20161015_20161021-104363-h360-n1-test',
      model_key: 'scat-comparison-v1',
      model_version: '1',
      dataset: 'scat',
      operation: 'run_model_comparison',
      title: '四方法轨迹预测对比',
      batch: BATCH,
      external_id: '104363',
      snapshot_count: 6,
      created_at: '2026-09-22T03:56:27Z',
      expires_at: null,
    },
  ],
}

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/evaluation', component: EvaluationView },
      {
        path: '/flights/:batch/:externalId',
        name: 'flight-detail',
        component: { template: '<div />' },
      },
      {
        path: '/visualizations/:artifactId/3d',
        name: 'visualization-3d',
        component: { template: '<div />' },
      },
    ],
  })
  await router.push('/evaluation')
  await router.isReady()
  const pinia = createPinia()
  useFilterStore(pinia).batch = BATCH

  const wrapper = mount(EvaluationView, {
    global: {
      plugins: [pinia, router, ElementPlus],
      stubs: { BaseChart: true },
    },
  })
  await flushPromises()
  return wrapper
}

describe('EvaluationView', () => {
  beforeEach(() => {
    vi.mocked(listBatches).mockResolvedValue({
      pagination: { offset: 0, limit: 100, returned: 1, total: 1, has_more: false },
      items: [batch],
    })
    vi.mocked(listMetricSummaries).mockResolvedValue(summaries)
    vi.mocked(listModelExperiments).mockResolvedValue(modelExperiments)
    vi.mocked(listWorstPredictionSamples).mockResolvedValue(worst)
    vi.mocked(listAnalysisArtifacts).mockResolvedValue(comparisonArtifacts)
  })

  it('renders metric summaries, KPI cards and worst cases from the M5-08 routes', async () => {
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('误差随时域变化')
    expect(wrapper.text()).toContain('指标汇总')
    expect(wrapper.text()).toContain('最差案例')
    expect(wrapper.text()).toContain('水平 MAE')
    expect(wrapper.text()).toContain('scat-prediction-metrics-v2')
    expect(wrapper.text()).toContain('整批次：all')
    expect(wrapper.text()).toContain('108567')
    expect(wrapper.text()).toContain('高空巡航')
    expect(wrapper.text()).toContain('176.91 km')
    expect(wrapper.text()).toContain('典型案例回放')
    expect(wrapper.text()).toContain('打开误差回放')
    expect(wrapper.text()).toContain('6 个快照')

    const replayLink = wrapper.get('.replay-link')
    expect(replayLink.attributes('href')).toContain(
      '/visualizations/model_comparison-scat-comparison-v1-scat-scat20161015_20161021-104363-h360-n1-test/3d',
    )

    expect(listMetricSummaries).toHaveBeenCalledWith(
      BATCH,
      { limit: 500 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(listWorstPredictionSamples).toHaveBeenCalledWith(
      BATCH,
      { metric: 'horizontal', horizon_s: 300, limit: 10 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(listAnalysisArtifacts).toHaveBeenCalledWith(
      { operation: 'run_model_comparison', batch: BATCH, limit: 5 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('filters the summary table by group locally without refetching', async () => {
    const wrapper = await mountView()
    vi.mocked(listMetricSummaries).mockClear()

    // 默认按整批次分组：阶段行不出现
    expect(wrapper.text()).toContain('整批次：all')
    expect(wrapper.text()).not.toContain('主导阶段：high_cruise')

    const groupSelect = wrapper
      .findAllComponents({ name: 'ElSelect' })
      .find((component) => component.props('modelValue') === 'overall')
    expect(groupSelect).toBeDefined()
    groupSelect?.vm.$emit('update:modelValue', 'phase')
    await flushPromises()

    expect(wrapper.text()).toContain('主导阶段：high_cruise')
    expect(wrapper.text()).not.toContain('整批次：all')
    // 汇总只取一次（limit 500），切分组在本地完成
    expect(listMetricSummaries).not.toHaveBeenCalled()
  })

  it('refetches worst cases when the horizon changes', async () => {
    const wrapper = await mountView()
    vi.mocked(listWorstPredictionSamples).mockClear()

    const tenMinutes = wrapper
      .findAll('.granularity-switch button')
      .find((node) => node.text().includes('10 分钟'))
    expect(tenMinutes).toBeDefined()
    await tenMinutes?.trigger('click')
    await flushPromises()

    expect(listWorstPredictionSamples).toHaveBeenCalledWith(
      BATCH,
      expect.objectContaining({ horizon_s: 600 }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('renders the self-developed model comparison with the 口径 disclaimer', async () => {
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('自研模型对比')
    expect(wrapper.text()).toContain('Kalman 常速度')
    expect(wrapper.text()).toContain('匀速-恒航向')
    expect(wrapper.text()).toContain('CFPL 基准（v2）')
    // 数值与样本量同格展示，模型口径按米制格式化
    expect(wrapper.text()).toContain('463.3 m')
    expect(wrapper.text()).toContain('2,052')
    // 口径声明存在，避免把两套体系混算
    expect(wrapper.text()).toContain('不可混算')
    // pending / 无指标实验不进入对比列
    expect(wrapper.text()).not.toContain('ghost-model')

    expect(listModelExperiments).toHaveBeenCalledWith(
      { batch: BATCH, limit: 20 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('shows the empty state when the batch has no model experiments', async () => {
    vi.mocked(listModelExperiments).mockResolvedValue({
      batch: BATCH,
      predictor_id: null,
      count: 0,
      experiments: [],
    })

    const wrapper = await mountView()

    expect(wrapper.text()).toContain('该批次还没有可用的自研模型实验')
    expect(wrapper.text()).not.toContain('Kalman 常速度')
  })
})
