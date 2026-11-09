import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/api/workspace'
import RunEvidencePanel from './RunEvidencePanel.vue'
import fixture from '@/test-fixtures/analysis.json'
vi.mock('@/api/workspace', () => ({
  listRunResults: vi.fn(),
  readRunResult: vi.fn(),
  readTrajectoryView: vi.fn(),
}))
const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55'
const reference = {
  result_id: 'a'.repeat(32),
  payload_sha256: 'b'.repeat(64),
  created_at: 1,
  expires_at: 9999999999,
  operation: 'search_scat_flights',
  kind: 'readonly_tool_result',
}
beforeEach(() => vi.clearAllMocks())
describe('Run evidence', () => {
  it('opens verified UAV plots on completion and restores them using reads alone', async () => {
    const source = { ...reference, operation: 'get_uav_samples' }
    const discovery = { ...reference, operation: 'search_uav_flights', result_id: 'c'.repeat(32) }
    vi.mocked(api.listRunResults).mockResolvedValue([discovery, source])
    const analysis = structuredClone(fixture.analysis_result)
    Object.assign(analysis.context, {
      readonly_result_id: source.result_id,
      readonly_payload_sha256: source.payload_sha256,
    })
    vi.mocked(api.readRunResult).mockImplementation(async (_t, _r, ref) => {
      const projected = structuredClone(analysis)
      projected.operation = ref.operation
      Object.assign(projected.context, { readonly_result_id: ref.result_id })
      if (ref.result_id === discovery.result_id)
        projected.blocks = projected.blocks.filter((b) => b.type !== 'trajectory_2d')
      return {
        ...ref,
        result: { status: 'ok', query: { external_id: 'SYNTH-UAV' }, data: {} },
        analysis_result: projected,
      }
    })
    const mountPanel = () =>
      mount(RunEvidencePanel, {
        props: { threadId: tid, runId: 'run_uav', status: '已完成', autoPlot: true },
        global: { stubs: { AnalysisResultRenderer: true } },
      })
    const first = mountPanel()
    await flushPromises()
    expect(first.find('[aria-label="本轮主结果"]').exists()).toBe(true)
    expect((first.get('select[aria-label="展示范围"]').element as HTMLSelectElement).value).toBe(
      source.result_id,
    )
    expect(api.readTrajectoryView).not.toHaveBeenCalled()
    first.unmount()
    const restored = mountPanel()
    await flushPromises()
    expect(restored.find('[aria-label="本轮主结果"]').exists()).toBe(true)
    expect(api.readRunResult).toHaveBeenCalledTimes(4)
    restored.unmount()
  })
  it('never claims full display when one referenced display page cannot be verified', async () => {
    const seed = {
      ...reference,
      operation: 'get_scat_track_points',
      trajectory_view_available: true,
    }
    const page = { ...seed, result_id: 'c'.repeat(32) }
    const missing = { ...seed, result_id: 'd'.repeat(32) }
    vi.mocked(api.listRunResults).mockResolvedValue([seed])
    vi.mocked(api.readRunResult).mockImplementation(async (_t, _r, ref) => {
      if (ref.result_id === missing.result_id) throw new Error('unavailable')
      const analysis = structuredClone(fixture.analysis_result)
      Object.assign(analysis.context, {
        readonly_result_id: ref.result_id,
        readonly_payload_sha256: ref.payload_sha256,
      })
      return {
        ...ref,
        result: {
          status: 'ok',
          query: { batch: 'SYNTH', external_id: '100002' },
          data: {},
          display_only: ref.result_id === page.result_id,
        },
        analysis_result: analysis,
      }
    })
    vi.mocked(api.readTrajectoryView).mockResolvedValue({
      items: [page, missing],
      coverage: {
        total: 524,
        loaded: 524,
        every_nth: 1,
        complete: true,
        full_extent: true,
        partial: false,
      },
    })
    const wrapper = mount(RunEvidencePanel, {
      props: { threadId: tid, runId: 'run_partial', status: '已完成', autoPlot: true },
      global: { stubs: { AnalysisResultRenderer: true } },
    })
    await flushPromises()
    expect(wrapper.text()).not.toContain('地图展示 524/524')
    expect(wrapper.get('[role="alert"]').text()).toContain('全程轨迹暂时不可用')
    expect(wrapper.findAll('.source-details article')).toHaveLength(1)
    wrapper.unmount()
  })
  it('automatically restores a supported plotting result and requests a cached bounded view', async () => {
    const source = {
      ...reference,
      operation: 'get_scat_track_points',
      trajectory_view_available: true,
    }
    const display = { ...source, result_id: 'c'.repeat(32), payload_sha256: 'd'.repeat(64) }
    const other = { ...source, result_id: 'e'.repeat(32), payload_sha256: 'f'.repeat(64) }
    vi.mocked(api.listRunResults).mockResolvedValue([source, other])
    vi.mocked(api.readRunResult).mockImplementation(async (_thread, _run, ref) => {
      const analysis = structuredClone(fixture.analysis_result)
      Object.assign(analysis.context, {
        readonly_result_id: ref.result_id,
        readonly_payload_sha256: ref.payload_sha256,
      })
      return {
        ...ref,
        result: {
          status: 'ok',
          query: {
            batch: 'SYNTH',
            external_id: ref.result_id === other.result_id ? '100200' : '100002',
          },
          data: {},
          display_only: ref.result_id === display.result_id,
        },
        analysis_result: analysis,
      }
    })
    vi.mocked(api.readTrajectoryView).mockResolvedValue({
      items: [display],
      coverage: {
        total: 524,
        loaded: 524,
        every_nth: 1,
        complete: true,
        full_extent: true,
        partial: false,
      },
    })
    const wrapper = mount(RunEvidencePanel, {
      props: { threadId: tid, runId: 'run_auto', status: '已完成', autoPlot: true },
      global: { stubs: { AnalysisResultRenderer: true } },
    })
    await flushPromises()
    expect(api.readTrajectoryView).toHaveBeenCalledWith(
      tid,
      'run_auto',
      expect.objectContaining({ result_id: source.result_id }),
      expect.anything(),
    )
    expect(wrapper.text()).toContain('地图展示 524/524 点')
    expect(wrapper.findAll('select[aria-label="展示范围"] option')).toHaveLength(2)
    expect(wrapper.find('[aria-label="本轮主结果"]').exists()).toBe(true)
    expect(wrapper.get('details.source-details').attributes('open')).toBeUndefined()
    wrapper.unmount()
  })
  it('explains an empty source using the execution status, without claiming a receipt failure', async () => {
    vi.mocked(api.listRunResults).mockResolvedValue([])
    const wrapper = mount(RunEvidencePanel, {
      props: { threadId: tid, runId: 'run_failed', status: '执行失败' },
    })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('本次指令执行失败')
    expect(wrapper.text()).not.toContain('暂无可读取的只读回执')
    await wrapper.setProps({ status: '对话已停止' })
    expect(wrapper.get('[role="status"]').text()).toContain('本次指令已停止')
    await wrapper.setProps({ status: '已完成' })
    expect(wrapper.get('[role="status"]').text()).toContain('没有独立的只读数据回执')
    expect(api.readRunResult).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('renders verified receipt charts and preserves the raw source', async () => {
    vi.mocked(api.listRunResults).mockResolvedValue([reference])
    const analysis = structuredClone(fixture.analysis_result)
    Object.assign(analysis.context, {
      readonly_result_id: reference.result_id,
      readonly_payload_sha256: reference.payload_sha256,
    })
    vi.mocked(api.readRunResult).mockResolvedValue({
      ...reference,
      result: { status: 'ok', data: { source_marker: 42 } },
      analysis_result: analysis,
    })
    const wrapper = mount(RunEvidencePanel, {
      props: { threadId: tid, runId: 'run_one' },
      global: { stubs: { BaseChart: true } },
    })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    await wrapper.findAll('button')[1]!.trigger('click')
    await flushPromises()
    expect(wrapper.find('[aria-label="结构化分析结果"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('分页样本仅代表已加载范围')
    expect(wrapper.get('pre').text()).toContain('source_marker')
    expect(api.readRunResult).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('refuses a chart projected from a different receipt', async () => {
    vi.mocked(api.listRunResults).mockResolvedValue([reference])
    const analysis = structuredClone(fixture.analysis_result)
    Object.assign(analysis.context, {
      readonly_result_id: 'other',
      readonly_payload_sha256: reference.payload_sha256,
    })
    vi.mocked(api.readRunResult).mockResolvedValue({
      ...reference,
      result: {},
      analysis_result: analysis,
    })
    const wrapper = mount(RunEvidencePanel, { props: { threadId: tid, runId: 'run_one' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    await wrapper.findAll('button')[1]!.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('来源暂时不可用')
    expect(wrapper.find('[aria-label="结构化分析结果"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('does not read business data until the user expands a source', async () => {
    vi.mocked(api.listRunResults).mockResolvedValue([reference])
    const wrapper = mount(RunEvidencePanel, { props: { threadId: tid, runId: 'run_one' } })
    expect(api.listRunResults).not.toHaveBeenCalled()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.listRunResults).toHaveBeenCalledWith(tid, 'run_one', expect.anything())
    expect(api.readRunResult).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('discards a late result after switching conversations', async () => {
    vi.mocked(api.listRunResults).mockResolvedValue([reference])
    let resolve!: (value: api.ResultReference & { result: unknown }) => void
    vi.mocked(api.readRunResult).mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const wrapper = mount(RunEvidencePanel, { props: { threadId: tid, runId: 'run_one' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    await wrapper.findAll('button')[1]!.trigger('click')
    await wrapper.setProps({ runId: 'run_two' })
    resolve({ ...reference, result: { private_old_thread: '不应显示' } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('不应显示')
    expect(wrapper.find('pre').exists()).toBe(false)
    wrapper.unmount()
  })
})
