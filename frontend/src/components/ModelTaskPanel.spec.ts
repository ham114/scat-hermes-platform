import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/api/hermes'
import type { ModelTask } from '@/api/hermes'
import ModelTaskPanel from './ModelTaskPanel.vue'
import fixture from '@/test-fixtures/analysis.json'

vi.mock('@/api/hermes', () => ({
  listModelTasks: vi.fn(),
  confirmModelTask: vi.fn(),
  revokeModelTask: vi.fn(),
  getModelTaskArtifact: vi.fn(),
  getSavedModelOutput: vi.fn(),
  reconcileModelTask: vi.fn(),
  cancelModelTask: vi.fn(),
}))
const task: ModelTask = {
  task_id: 'a'.repeat(32),
  run_id: 'b'.repeat(32),
  state: 'pending',
  snapshot_sha256: 'c'.repeat(64),
  expires_at: 9999999999,
  parameters: { horizon: 36 },
  input_refs: [{ external_id: 'flight-1', batch: 'batch' }],
  evaluator: { key: 'eval', version: '1' },
  components: { feature_pipeline: { key: 'prep', version: '1' } },
  code_version: 'h5',
  model: {
    display_name: '测试模型',
    model_key: 'test-model',
    version: '1',
    limits: { max_batch_size: 1 },
  },
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.listModelTasks).mockResolvedValue({ items: [structuredClone(task)] })
  vi.mocked(api.confirmModelTask).mockResolvedValue({ ...task, state: 'queued' })
})

describe('explicit model task confirmation', () => {
  it('identifies the actual GBAR model and preprocessing without confusing workflow version with model version', async () => {
    vi.mocked(api.listModelTasks).mockResolvedValue({
      items: [
        {
          ...task,
          workflow: {
            proposal: { kind: 'forecast', models: [{ model: 'scat-gbar-v23', version: '23' }] },
            gpu: { device: 'cuda', release_id: 'reviewed-gbar-release' },
            coverage: { selected_count: 1 },
            trials: [{ trial_id: 'one' }],
            synthetic: false,
            tool_groups: {},
            input_sha256: 'd'.repeat(64),
          },
        },
      ],
    })
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'gbar' } })
    await flushPromises()
    expect(wrapper.get('h3').text()).not.toContain('· 1')
    expect(wrapper.text()).toContain('scat-gbar-v23:23')
    expect(wrapper.text()).toContain('reviewed-gbar-release')
    expect(wrapper.text()).toContain('36个输入时刻')
    expect(api.confirmModelTask).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('labels a forecast overview and its trajectory separately while retaining one preview area', async () => {
    vi.mocked(api.listModelTasks).mockResolvedValue({
      items: [
        {
          ...task,
          state: 'finished',
          workflow: {
            proposal: { kind: 'forecast' },
            coverage: { selected_count: 1 },
            trials: [{ trial_id: 'one' }],
            synthetic: true,
            tool_groups: {},
            input_sha256: 'd'.repeat(64),
          },
          run: {
            status: 'succeeded',
            progress: 1,
            result_artifact_ids: ['overview', 'trajectory'],
          },
        },
      ],
    })
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'forecast' } })
    await flushPromises()
    expect(wrapper.findAll('button').filter((b) => b.text() === '查看预测摘要')).toHaveLength(1)
    expect(wrapper.findAll('button').filter((b) => b.text() === '查看预测轨迹 1')).toHaveLength(1)
    expect(api.confirmModelTask).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('shows the exact approved observation count, origin and absence of future truth', async () => {
    vi.mocked(api.listModelTasks).mockResolvedValue({
      items: [
        {
          ...task,
          evaluator: null,
          workflow: {
            proposal: { kind: 'forecast' },
            coverage: { selected_count: 1 },
            trials: [{ trial_id: 'trial-1' }],
            synthetic: false,
            tool_groups: {},
            input_sha256: 'd'.repeat(64),
            prediction_contract: {
              mode: 'future_forecast',
              observation_last_n: 200,
              windows: [
                {
                  external_id: '100002',
                  point_count: 200,
                  start_time: '2020-01-01T00:00:00Z',
                  forecast_origin: '2020-01-01T00:15:00Z',
                  span_s: 900,
                },
              ],
            },
          },
        },
      ],
    })
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'forecast' } })
    await flushPromises()
    expect(wrapper.text()).toContain('从末次观测向未来预测')
    expect(wrapper.text()).toContain('精确使用最后 200 点')
    expect(wrapper.text()).toContain('2020-01-01T00:15:00Z')
    expect(wrapper.text()).toContain('不计算误差指标')
    expect(api.confirmModelTask).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('reads an owned artifact for preview without confirming or recomputing', async () => {
    vi.mocked(api.listModelTasks).mockResolvedValue({
      items: [
        {
          ...task,
          state: 'finished',
          run: { status: 'succeeded', progress: 1, result_artifact_ids: [fixture.artifact_id] },
        },
      ],
    })
    vi.mocked(api.getModelTaskArtifact).mockResolvedValue(structuredClone(fixture))
    const wrapper = mount(ModelTaskPanel, {
      props: { threadId: 'thread' },
      global: { stubs: { BaseChart: true } },
    })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '查看分析结果')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('H6 合成结果协议展示')
    expect(api.getModelTaskArtifact).toHaveBeenCalledWith(
      'thread',
      task.task_id,
      fixture.artifact_id,
    )
    expect(api.confirmModelTask).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('drops stale artifact responses after a thread switch', async () => {
    vi.mocked(api.listModelTasks).mockResolvedValue({
      items: [
        {
          ...task,
          state: 'finished',
          run: { status: 'succeeded', progress: 1, result_artifact_ids: [fixture.artifact_id] },
        },
      ],
    })
    let resolve!: (value: Record<string, unknown>) => void
    vi.mocked(api.getModelTaskArtifact).mockReturnValueOnce(
      new Promise((r) => {
        resolve = r
      }),
    )
    const wrapper = mount(ModelTaskPanel, {
      props: { threadId: 'old-thread' },
      global: { stubs: { BaseChart: true } },
    })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '查看分析结果')!
      .trigger('click')
    vi.mocked(api.listModelTasks).mockResolvedValue({ items: [] })
    await wrapper.setProps({ threadId: 'new-thread' })
    resolve(structuredClone(fixture))
    await flushPromises()
    expect(wrapper.text()).not.toContain('H6 合成结果协议展示')
    wrapper.unmount()
  })
  it('shows workflow groups, partial outcome, steps and cumulative budget', async () => {
    vi.mocked(api.listModelTasks).mockResolvedValue({
      items: [
        {
          ...task,
          state: 'finished',
          workflow: {
            proposal: { kind: 'comparison' },
            coverage: { selected_count: 2 },
            trials: [{ trial_id: 'trial-one' }],
            synthetic: true,
            input_sha256: 'd'.repeat(64),
            tool_groups: {
              scat_experiment_steps: ['scat_trial_run'],
              scat_evaluation_steps: ['scat_evaluation_summarize'],
            },
          },
          skill_job: {
            state: 'partial',
            budget: { tool_calls: 12 },
            steps: [{ operation: 'scat_trial_run', state: 'succeeded' }],
            backend_executions: [{ id: 'one', state: 'succeeded', calls: 1 }],
          },
        },
      ],
    })
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'thread' } })
    await flushPromises()
    expect(wrapper.text()).toContain('部分成功')
    expect(wrapper.get('[aria-label="业务工具组"]').text()).toContain('评估诊断')
    expect(wrapper.text()).toContain('scat_trial_run')
    expect(wrapper.text()).toContain('tool_calls')
    expect(wrapper.text()).not.toContain('确认参数并执行')
    wrapper.unmount()
  })
  it('restores pending parameters without submitting and confirms only on click', async () => {
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'thread-one' } })
    await flushPromises()
    expect(api.confirmModelTask).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('flight-1')
    expect(wrapper.text()).toContain('prep')
    const button = wrapper.findAll('button').find((b) => b.text() === '确认参数并执行')!
    await button.trigger('click')
    await flushPromises()
    expect(api.confirmModelTask).toHaveBeenCalledExactlyOnceWith('thread-one', task)
    wrapper.unmount()
  })
  it('unknown submission requires querying state before another click', async () => {
    vi.mocked(api.confirmModelTask).mockRejectedValueOnce(new Error('network'))
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'thread' } })
    await flushPromises()
    const button = wrapper.findAll('button').find((b) => b.text() === '确认参数并执行')!
    await button.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role=alert]').text()).toContain('刷新')
    expect(button.attributes('disabled')).toBeDefined()
    expect(api.confirmModelTask).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('shows unknown execution without an execution or revoke action', async () => {
    vi.mocked(api.listModelTasks).mockResolvedValue({ items: [{ ...task, state: 'unknown' }] })
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'thread' } })
    await flushPromises()
    expect(wrapper.text()).toContain('禁止自动重跑')
    expect(wrapper.text()).not.toContain('确认参数并执行')
    expect(wrapper.text()).not.toContain('撤销未执行任务')
    wrapper.unmount()
  })
  it('ignores stale task responses after switching threads', async () => {
    let resolve!: (value: { items: ModelTask[] }) => void
    vi.mocked(api.listModelTasks).mockReturnValueOnce(
      new Promise((r) => {
        resolve = r
      }),
    )
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'old-thread' } })
    vi.mocked(api.listModelTasks).mockResolvedValueOnce({ items: [] })
    await wrapper.setProps({ threadId: 'new-thread' })
    await flushPromises()
    resolve({ items: [task] })
    await flushPromises()
    expect(wrapper.text()).not.toContain('flight-1')
    expect(api.confirmModelTask).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('removes old task actions immediately when the next task list cannot be read', async () => {
    const wrapper = mount(ModelTaskPanel, { props: { threadId: 'old-thread' } })
    await flushPromises()
    expect(wrapper.text()).toContain('确认参数并执行')
    vi.mocked(api.listModelTasks).mockRejectedValueOnce(new Error('network'))
    await wrapper.setProps({ threadId: 'new-thread' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('确认参数并执行')
    expect(wrapper.text()).not.toContain('flight-1')
    expect(wrapper.get('[role=alert]').text()).toContain('任务状态暂时不可用')
    expect(api.confirmModelTask).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
