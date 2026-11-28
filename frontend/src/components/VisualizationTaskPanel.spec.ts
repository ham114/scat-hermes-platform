import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { HermesApiError } from '@/api/hermes'
import { readMemoryVersion } from '@/api/memory'
import { listVisualizations, readVisualization, reviseVisualization } from '@/api/visualizations'
import VisualizationTaskPanel from './VisualizationTaskPanel.vue'
import VisualizationArtifactView from './VisualizationArtifactView.vue'

vi.mock('@/api/visualizations', () => ({
  listVisualizations: vi.fn(),
  readVisualization: vi.fn(),
  reviseVisualization: vi.fn(),
  visualizationSources: vi.fn(),
  createVisualization: vi.fn(),
  cancelVisualization: vi.fn(),
  visualizationFile: (t: string, a: string, f: string) => `/files/${t}/${a}/${f}`,
}))
vi.mock('@/api/memory', () => ({ readMemoryVersion: vi.fn() }))
const artifact = {
  artifact_id: 'a'.repeat(32),
  task_id: 'b'.repeat(32),
  version: 1,
  intent: '原始图表',
  viewports: ['desktop', 'mobile'],
  data_refs: [],
  files: {},
}
const task = {
  task_id: artifact.task_id,
  artifact_id: artifact.artifact_id,
  state: 'succeeded',
  phase: 'publishing',
  version: 1,
  intent: artifact.intent,
  created_at: 1,
}
beforeEach(() => {
  sessionStorage.clear()
  vi.resetAllMocks()
  vi.mocked(listVisualizations).mockResolvedValue({ enabled: true, items: [task] } as never)
  vi.mocked(readVisualization).mockResolvedValue(artifact as never)
})
describe('autonomous chart delivery', () => {
  it('submits the generic memory version with feedback without specialized endpoints', async () => {
    vi.mocked(readMemoryVersion).mockResolvedValue({ enabled: true, epoch: 3, revision: 8 })
    vi.mocked(reviseVisualization).mockResolvedValue(task as never)
    const wrapper = mount(VisualizationTaskPanel, { props: { threadId: 'thread' } })
    await flushPromises()
    await wrapper.get('textarea').setValue('后续高度默认使用米')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(vi.mocked(reviseVisualization).mock.calls[0]![2]).toMatchObject({
      feedback: '后续高度默认使用米',
      memory_epoch: 3,
      memory_revision: 8,
    })
    expect(readMemoryVersion).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('distinguishes chart delivery from an unconfirmed memory result', async () => {
    vi.mocked(listVisualizations).mockResolvedValue({
      enabled: true,
      items: [{ ...task, memory_outcome: 'unconfirmed' }],
    } as never)
    const wrapper = mount(VisualizationTaskPanel, { props: { threadId: 'thread' } })
    await flushPromises()
    expect(wrapper.text()).toContain('未收到完整的偏好确认，未保存新的长期偏好')
    expect(wrapper.text()).toContain('原始图表')
    expect(wrapper.text()).not.toContain('本次明确的长期偏好已保存')
    wrapper.unmount()
  })
  it('retains the request ID after an unknown POST and never automatically retries', async () => {
    vi.mocked(reviseVisualization)
      .mockRejectedValueOnce(new HermesApiError('unknown', true))
      .mockResolvedValueOnce(task as never)
    const wrapper = mount(VisualizationTaskPanel, { props: { threadId: 'thread' } })
    await flushPromises()
    await wrapper.get('textarea').setValue('增大图例字号')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(reviseVisualization).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('入队结果尚未确认')
    const request = vi.mocked(reviseVisualization).mock.calls[0]![2]
    const recover = wrapper.findAll('button').find((b) => b.text() === '恢复原绘图请求')!
    await recover.trigger('click')
    await flushPromises()
    expect(reviseVisualization).toHaveBeenCalledTimes(2)
    expect(vi.mocked(reviseVisualization).mock.calls[1]![2]).toEqual(request)
    expect(wrapper.text()).toContain('原始图表')
    wrapper.unmount()
  })
  it('keeps history readable while creation and feedback are disabled', async () => {
    vi.mocked(listVisualizations).mockResolvedValue({ enabled: false, items: [task] } as never)
    const wrapper = mount(VisualizationTaskPanel, { props: { threadId: 'thread' } })
    await flushPromises()
    expect(wrapper.text()).toContain('原始图表')
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('根据会话数据绘图')
    expect(reviseVisualization).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('discards an old artifact response after the thread changes', async () => {
    let resolveOld!: (value: never) => void
    vi.mocked(readVisualization).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve
        }),
    )
    const wrapper = mount(VisualizationArtifactView, {
      props: { threadId: 'old', artifactId: 'a'.repeat(32) },
    })
    await wrapper.setProps({ threadId: 'new', artifactId: 'c'.repeat(32) })
    await flushPromises()
    resolveOld({ ...artifact, intent: '禁止显示的旧会话图表' } as never)
    await flushPromises()
    expect(wrapper.text()).not.toContain('禁止显示的旧会话图表')
    wrapper.unmount()
  })
})
