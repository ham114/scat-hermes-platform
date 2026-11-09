import ElementPlus from 'element-plus'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, reactive } from 'vue'

import { useTimeCursorStore } from '@/stores/timeCursor'
import VisualizationEntryView from './VisualizationEntryView.vue'

const route = reactive({
  params: { artifactId: 'a'.repeat(32) },
  query: { thread_id: 'untrusted-query-thread' },
})
const apiMocks = vi.hoisted(() => ({ getAnalysisArtifact: vi.fn() }))
const reportMocks = vi.hoisted(() => ({ downloadComparisonSnapshotReport: vi.fn() }))
let pinia: Pinia

const Trajectory3dViewStub = defineComponent({
  name: 'Trajectory3dView',
  props: {
    tracks: { type: Array, default: () => [] },
    errorSelection: { type: Object, default: null },
  },
  template: '<section class="trajectory-3d-stub"><slot name="playback" /></section>',
})

vi.mock('vue-router', () => ({ useRoute: () => route }))
vi.mock('@/api/client', () => ({ getAnalysisArtifact: apiMocks.getAnalysisArtifact }))
vi.mock('@/utils/comparisonReport', () => ({
  downloadComparisonSnapshotReport: reportMocks.downloadComparisonSnapshotReport,
}))

const artifact = {
  schema_version: '1.0',
  artifact_id: 'a'.repeat(32),
  run_id: 'b'.repeat(32),
  kind: 'analysis_result',
  thread_id: 'trusted-artifact-thread',
  model_key: 'scat-gbar-v23',
  model_version: '23',
  analysis_result: {
    schema_version: '1.4',
    dataset: 'scat',
    operation: 'run_regression_model',
    title: 'GBAR v23 回归预测结果',
    context: {},
    blocks: [{ type: 'metrics', title: '摘要' }],
  },
  created_at: '2026-09-16T08:00:00Z',
}

describe('VisualizationEntryView', () => {
  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.stubGlobal(
      'requestAnimationFrame',
      vi.fn(() => 1),
    )
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    route.params.artifactId = 'a'.repeat(32)
    route.query.thread_id = 'untrusted-query-thread'
    apiMocks.getAnalysisArtifact.mockReset()
    apiMocks.getAnalysisArtifact.mockResolvedValue(artifact)
    reportMocks.downloadComparisonSnapshotReport.mockReset()
  })

  it('loads the owner-scoped artifact and trusts its stored conversation id', async () => {
    const wrapper = mount(VisualizationEntryView, {
      global: { plugins: [pinia, ElementPlus], stubs: { RouterLink: true } },
    })
    await flushPromises()

    expect(apiMocks.getAnalysisArtifact).toHaveBeenCalledWith('a'.repeat(32))
    expect(wrapper.text()).toContain('GBAR 轨迹预测结果')
    expect(wrapper.text()).toContain('GBAR v23')
    expect(wrapper.text()).not.toContain('a'.repeat(32))
    expect(wrapper.text()).not.toContain('trusted-artifact-thread')
    expect(wrapper.text()).not.toContain('untrusted-query-thread')
    expect(wrapper.get('a.primary-link').attributes('href')).toBe(
      'http://localhost:8501/?thread_id=trusted-artifact-thread',
    )
  })

  it('rejects malformed artifact ids without issuing a request', async () => {
    route.params.artifactId = '../../secret'
    const wrapper = mount(VisualizationEntryView, {
      global: { plugins: [pinia, ElementPlus], stubs: { RouterLink: true } },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('结果链接无效')
    expect(apiMocks.getAnalysisArtifact).not.toHaveBeenCalled()
  })

  it('keeps rendering diagnostics out of the user page while preserving downsampling', async () => {
    apiMocks.getAnalysisArtifact.mockResolvedValue({
      ...artifact,
      analysis_result: {
        ...artifact.analysis_result,
        blocks: [
          {
            type: 'trajectory_2d',
            title: '大轨迹',
            x_key: 'longitude',
            x_label: '经度',
            y_key: 'latitude',
            y_label: '纬度',
            time_key: 'time_step_s',
            series: [],
            points: Array.from({ length: 2_001 }, (_, index) => ({
              time_step_s: index,
              longitude: 10 + index * 0.0001,
              latitude: 50 + Math.sin(index / 50) * 0.01,
            })),
          },
        ],
      },
    })

    const wrapper = mount(VisualizationEntryView, {
      global: {
        plugins: [pinia, ElementPlus],
        stubs: {
          RouterLink: true,
          Trajectory2dPlayback: true,
          TrajectoryProfileCharts: true,
          Trajectory3dView: Trajectory3dViewStub,
        },
      },
    })
    await flushPromises()

    expect(wrapper.text()).not.toContain('trajectory-render-v1')
    expect(wrapper.text()).not.toContain('segment-aware-spatiotemporal-max-error-v1')
    expect(wrapper.text()).not.toContain('有效源点')
    const renderedTracks = wrapper.findComponent({ name: 'Trajectory2dPlayback' }).props('tracks')
    expect(renderedTracks[0].points).toHaveLength(2_000)
  })

  it('passes distinct comparison trajectories to each user-facing view', async () => {
    const methodIds = ['actual', 'scat-gbar-v23', 'kalman-cv', 'constant-velocity-heading', 'cfpl']
    apiMocks.getAnalysisArtifact.mockResolvedValue({
      ...artifact,
      analysis_result: {
        ...artifact.analysis_result,
        operation: 'run_model_comparison',
        blocks: [
          {
            type: 'trajectory_2d',
            title: '实际轨迹与多方法预测',
            x_key: 'longitude',
            x_label: '经度',
            y_key: 'latitude',
            y_label: '纬度',
            time_key: 'time_step_s',
            path_key: 'trajectory_id',
            path_label: '轨迹方法',
            segment_key: 'segment',
            segment_label: '轨迹类型',
            series: [],
            points: methodIds.flatMap((trajectoryId, index) => [
              {
                time_step_s: 10,
                longitude: 10 + index,
                latitude: 50,
                trajectory_id: trajectoryId,
                segment: trajectoryId === 'actual' ? 'actual_future' : 'predicted',
              },
              {
                time_step_s: 20,
                longitude: 11 + index,
                latitude: 51,
                trajectory_id: trajectoryId,
                segment: trajectoryId === 'actual' ? 'actual_future' : 'predicted',
              },
            ]),
          },
        ],
      },
    })

    const wrapper = mount(VisualizationEntryView, {
      global: {
        plugins: [pinia, ElementPlus],
        stubs: {
          RouterLink: true,
          Trajectory2dPlayback: true,
          TrajectoryProfileCharts: true,
          Trajectory3dView: Trajectory3dViewStub,
        },
      },
    })
    await flushPromises()

    const labels = wrapper
      .findComponent({ name: 'Trajectory2dPlayback' })
      .props('tracks')
      .map((track: { label: string }) => track.label)
    expect(labels).toEqual([
      '实际轨迹',
      'GBAR v23',
      'Kalman 常速度',
      '匀速-恒航向',
      'CFPL 计划航路',
    ])
  })

  it('switches the tracks shared by 2D and 3D when the snapshot changes', async () => {
    const snapshots = ['flight-1 · 2026-09-15T00:00:00Z', 'flight-2 · 2026-09-15T01:00:00Z']
    apiMocks.getAnalysisArtifact.mockResolvedValue({
      ...artifact,
      analysis_result: {
        ...artifact.analysis_result,
        schema_version: '1.5',
        operation: 'run_model_comparison',
        blocks: [
          {
            type: 'trajectory_2d',
            title: '多快照',
            x_key: 'longitude',
            x_label: '经度',
            y_key: 'latitude',
            y_label: '纬度',
            time_key: 'time_step_s',
            path_key: 'trajectory_id',
            path_label: '方法',
            segment_key: 'segment',
            segment_label: '类型',
            snapshot_key: 'snapshot_id',
            snapshot_label: '快照',
            series: [],
            points: snapshots.flatMap((snapshot, index) => [
              {
                time_step_s: 10,
                longitude: 10 + index,
                latitude: 50,
                trajectory_id: 'actual',
                segment: 'actual_future',
                snapshot_id: snapshot,
              },
              {
                time_step_s: 20,
                longitude: 11 + index,
                latitude: 51,
                trajectory_id: 'actual',
                segment: 'actual_future',
                snapshot_id: snapshot,
              },
            ]),
          },
        ],
      },
    })

    const wrapper = mount(VisualizationEntryView, {
      global: {
        plugins: [pinia, ElementPlus],
        stubs: {
          RouterLink: true,
          Trajectory2dPlayback: true,
          TrajectoryProfileCharts: true,
          Trajectory3dView: Trajectory3dViewStub,
        },
      },
    })
    await flushPromises()

    const playback = wrapper.findComponent({ name: 'Trajectory2dPlayback' })
    const profiles = wrapper.findComponent({ name: 'TrajectoryProfileCharts' })
    expect(playback.props('tracks')[0].snapshotId).toBe(snapshots[0])
    expect(profiles.props('tracks')[0].snapshotId).toBe(snapshots[0])

    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', snapshots[1])
    await flushPromises()

    expect(playback.props('tracks')[0].snapshotId).toBe(snapshots[1])
    expect(profiles.props('tracks')[0].snapshotId).toBe(snapshots[1])
  })

  it('locates a backend-declared worst error on the shared cursor', async () => {
    const snapshot = 'flight-1 · 2026-09-15T00:00:00Z'
    apiMocks.getAnalysisArtifact.mockResolvedValue({
      ...artifact,
      analysis_result: {
        ...artifact.analysis_result,
        schema_version: '1.6',
        operation: 'run_model_comparison',
        blocks: [
          {
            type: 'table',
            title: '可视化快照逐方法指标',
            columns: [],
            rows: [
              {
                snapshot_id: snapshot,
                method_id: 'kalman-cv',
                ade_m: 1_000,
                fde_m: 2_000,
                max_error_m: 3_000,
                worst_time_s: 20,
              },
            ],
          },
          {
            type: 'trajectory_2d',
            title: '误差轨迹',
            x_key: 'longitude',
            x_label: '经度',
            y_key: 'latitude',
            y_label: '纬度',
            time_key: 'time_step_s',
            path_key: 'trajectory_id',
            path_label: '方法',
            segment_key: 'segment',
            segment_label: '类型',
            snapshot_key: 'snapshot_id',
            snapshot_label: '快照',
            series: [
              {
                key: 'horizontal_error_m',
                label: '水平误差',
                unit: 'm',
                role: 'color',
              },
            ],
            points: [10, 20].map((time, index) => ({
              time_step_s: time,
              longitude: 10 + index,
              latitude: 50 + index,
              trajectory_id: 'kalman-cv',
              segment: 'predicted',
              snapshot_id: snapshot,
              horizontal_error_m: index === 1 ? 3_000 : 100,
            })),
          },
        ],
      },
    })

    const wrapper = mount(VisualizationEntryView, {
      global: {
        plugins: [pinia, ElementPlus],
        stubs: {
          RouterLink: true,
          Trajectory2dPlayback: true,
          TrajectoryProfileCharts: true,
          Trajectory3dView: Trajectory3dViewStub,
        },
      },
    })
    await flushPromises()
    await wrapper.get('[aria-label="最差误差点定位"] button').trigger('click')
    await flushPromises()

    expect(useTimeCursorStore().currentTime).toBe(20)
    expect(wrapper.findComponent({ name: 'Trajectory2dPlayback' }).props('errorSelection')).toEqual(
      {
        trackId: `${snapshot}::kalman-cv`,
        time: 20,
        horizontalErrorMetres: 3_000,
      },
    )
    await wrapper.get('.result-actions button').trigger('click')
    expect(reportMocks.downloadComparisonSnapshotReport).toHaveBeenCalledWith(
      expect.objectContaining({
        snapshotId: snapshot,
        metrics: [expect.objectContaining({ methodId: 'kalman-cv', maxErrorMetres: 3_000 })],
      }),
    )
  })
})
