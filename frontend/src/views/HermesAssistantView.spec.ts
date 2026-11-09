import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import AnalysisResultRenderer from '@/components/AnalysisResultRenderer.vue'
import fixture from '@/test-fixtures/analysis.json'
import { parseAnalysisArtifact } from '@/lib/analysisDisplay'

import * as api from '@/api/hermes'
import type { HermesEvent } from '@/api/hermes'
import HermesAssistantView from './HermesAssistantView.vue'
import WorkflowCatalogPanel from '@/components/WorkflowCatalogPanel.vue'

vi.mock('@/api/hermes', async (original) => ({
  ...(await original<typeof import('@/api/hermes')>()),
  createHermesThread: vi.fn(),
  createHermesRun: vi.fn(),
  getHermesRun: vi.fn(),
  readHermesEvents: vi.fn(),
  stopHermesRun: vi.fn(),
  listModelTasks: vi.fn(),
  getWorkflowCatalog: vi.fn(),
  prepareWorkflow: vi.fn(),
}))
const tid = 'f4882ffa-7d95-4374-a628-b6978cef3ce9'
const rid = 'run_assistant'
const key = 'scat-hermes-conversation-v1'
beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  vi.mocked(api.listModelTasks).mockResolvedValue({ items: [] })
  vi.mocked(api.getWorkflowCatalog).mockResolvedValue({
    enabled: true,
    synthetic: true,
    groups: { scat_report_steps: ['scat_report_build', 'scat_report_read'] },
    control_group: {},
    model_ids: ['constant-velocity-heading'],
    production_gpu: false,
  })
  vi.mocked(api.createHermesThread).mockResolvedValue({ thread_id: tid })
  vi.mocked(api.createHermesRun).mockResolvedValue({ run_id: rid, status: 'started' })
  vi.mocked(api.getHermesRun).mockResolvedValue({ run_id: rid, status: 'running' })
  vi.mocked(api.readHermesEvents).mockImplementation(async (_tid, _rid, callback) => {
    callback({
      event: 'tool.started',
      run_id: rid,
      tool: 'mcp__scat__search_scat_flights',
      sequence: 0,
    })
    callback({ event: 'message.delta', run_id: rid, delta: '草稿', sequence: 1 })
    callback({
      event: 'run.completed',
      run_id: rid,
      output: '最终结果 <script>test</script>',
      sequence: 2,
    })
  })
})
async function send(wrapper: ReturnType<typeof mount>) {
  await wrapper.get('textarea').setValue('查询指定批次航班')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
}
describe('Hermes assistant', () => {
  it('handles a presentation request locally and clears targets on a new conversation', async () => {
    localStorage.clear()
    sessionStorage.setItem(key, JSON.stringify({ threadId: tid, messages: [] }))
    const result = parseAnalysisArtifact(fixture, fixture.artifact_id)!
    const wrapper = mount(HermesAssistantView, {
      global: {
        stubs: {
          BaseChart: true,
          ModelTaskPanel: defineComponent({
            setup: () => () =>
              h(AnalysisResultRenderer, { result, displayIdentity: 'chat-view:digest' }),
          }),
        },
      },
    })
    await wrapper.get('#assistant-input').setValue('请将当前结果改成双列。')
    await wrapper.get('.assistant-composer').trigger('submit')
    await flushPromises()
    expect(wrapper.get('.display-grid').classes()).toContain('dual')
    expect(wrapper.text()).toContain('展示已调整并保存')
    expect(api.createHermesRun).not.toHaveBeenCalled()
    expect(api.createHermesThread).not.toHaveBeenCalled()
    await wrapper.get('#assistant-input').setValue('把第二张图改成柱状图')
    await wrapper.get('.assistant-composer').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('当前支持')
    expect(api.createHermesRun).not.toHaveBeenCalled()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '新建会话')!
      .trigger('click')
    await wrapper.get('#assistant-input').setValue('请将当前结果改成单列。')
    await wrapper.get('.assistant-composer').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('先打开')
    expect(api.createHermesRun).not.toHaveBeenCalled()
    wrapper.unmount()
    localStorage.clear()
  })
  it('allows corrected parameters after an explicit server rejection', async () => {
    vi.mocked(api.prepareWorkflow)
      .mockRejectedValueOnce(new api.HermesApiError('invalid data', false))
      .mockResolvedValueOnce({ task_id: 'a'.repeat(32) } as api.ModelTask)
    const wrapper = mount(HermesAssistantView)
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('流程模板'))!
      .trigger('click')
    await flushPromises()
    const panel = wrapper.findComponent(WorkflowCatalogPanel)
    panel.vm.$emit('prepare', { kind: 'forecast', batch: 'wrong' }, {})
    await flushPromises()
    expect(wrapper.text()).not.toContain('核对并恢复原方案')
    expect(panel.props('disabled')).toBe(false)
    panel.vm.$emit('prepare', { kind: 'forecast', batch: 'corrected' }, {})
    await flushPromises()
    expect(api.prepareWorkflow).toHaveBeenCalledTimes(2)
    const calls = vi.mocked(api.prepareWorkflow).mock.calls
    expect(calls[1]![1]).toEqual({ kind: 'forecast', batch: 'corrected' })
    expect(calls[1]![2]!.requestId).not.toBe(calls[0]![2]!.requestId)
    expect(api.createHermesRun).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('direct preparation uses no planner and retries the same UUID after an uncertain response', async () => {
    vi.mocked(api.prepareWorkflow)
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce({ task_id: 'a'.repeat(32) } as api.ModelTask)
    const wrapper = mount(HermesAssistantView)
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('流程模板'))!
      .trigger('click')
    await flushPromises()
    const proposal = { kind: 'forecast', batch: 'batch' },
      budget = { max_llm_tokens: 430041 }
    wrapper.findComponent(WorkflowCatalogPanel).vm.$emit('prepare', proposal, budget)
    await flushPromises()
    const first = vi.mocked(api.prepareWorkflow).mock.calls[0]!
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '核对并恢复原方案')!
      .trigger('click')
    await flushPromises()
    expect(api.prepareWorkflow).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.prepareWorkflow).mock.calls[1]).toEqual(first)
    expect(api.createHermesThread).toHaveBeenCalledTimes(1)
    expect(api.createHermesRun).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('核对并恢复原方案')
    wrapper.unmount()
  })
  it('blocks conversation writes in a readonly preview', async () => {
    vi.stubEnv('VITE_WORKSPACE_READONLY', '1')
    const wrapper = mount(HermesAssistantView)
    try {
      await wrapper.get('textarea').setValue('不应提交')
      await wrapper.get('form').trigger('submit')
      await flushPromises()
      expect(api.createHermesRun).not.toHaveBeenCalled()
      expect(wrapper.get('textarea').attributes('disabled')).toBeDefined()
      expect(wrapper.text()).toContain('只读预览')
    } finally {
      wrapper.unmount()
      vi.unstubAllEnvs()
    }
  })
  it('sends a request, shows tool state, and renders output as plain text', async () => {
    const wrapper = mount(HermesAssistantView)
    await send(wrapper)
    expect(wrapper.text()).toContain('最终结果 <script>test</script>')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.text()).toContain('航班检索')
    expect(wrapper.text()).toContain('已完成')
    expect(JSON.parse(sessionStorage.getItem(key)!).pending).toBeUndefined()
    wrapper.unmount()
  })
  it('recovers an unknown admission with the same UUID and one user message', async () => {
    vi.mocked(api.createHermesRun).mockRejectedValueOnce(new api.HermesApiError('连接中断', true))
    const wrapper = mount(HermesAssistantView)
    await send(wrapper)
    expect(wrapper.get('[role=alert]').text()).toContain('中断')
    const old = vi.mocked(api.createHermesRun).mock.calls[0]![1]
    const recover = wrapper.findAll('button').find((button) => button.text() === '恢复本次请求')!
    await recover.trigger('click')
    await flushPromises()
    expect(vi.mocked(api.createHermesRun).mock.calls[1]![1]).toBe(old)
    expect(wrapper.findAll('.assistant-message.user')).toHaveLength(1)
    expect(wrapper.text()).toContain('已完成')
    wrapper.unmount()
  })
  it('restores an existing run without issuing a new POST and replaces partial text', async () => {
    sessionStorage.setItem(
      key,
      JSON.stringify({
        threadId: tid,
        messages: [
          { role: 'user', text: '问题' },
          { role: 'assistant', text: '之前的草稿' },
        ],
        pending: { requestId: crypto.randomUUID(), message: '问题', runId: rid, sequence: 5 },
      }),
    )
    const wrapper = mount(HermesAssistantView)
    await flushPromises()
    expect(api.createHermesRun).not.toHaveBeenCalled()
    expect(api.readHermesEvents).toHaveBeenCalledWith(
      tid,
      rid,
      expect.any(Function),
      expect.anything(),
      5,
    )
    expect(wrapper.text()).not.toContain('之前的草稿')
    expect(wrapper.text()).toContain('最终结果')
    wrapper.unmount()
  })
  it('does not automatically retry an unconfirmed POST on refresh', async () => {
    sessionStorage.setItem(
      key,
      JSON.stringify({
        messages: [{ role: 'user', text: '问题' }],
        pending: { requestId: crypto.randomUUID(), message: '问题', sequence: -1 },
      }),
    )
    const wrapper = mount(HermesAssistantView)
    await flushPromises()
    expect(api.createHermesRun).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('恢复本次请求')
    wrapper.unmount()
  })
  it('waits for the actual cancelled event and never labels cancellation completed', async () => {
    let callback: ((event: HermesEvent) => void) | undefined
    let done: (() => void) | undefined
    vi.mocked(api.readHermesEvents).mockImplementation(
      (_t, _r, receive) =>
        new Promise<void>((resolve) => {
          callback = receive
          done = resolve
        }),
    )
    vi.mocked(api.stopHermesRun).mockResolvedValue({ run_id: rid, status: 'stopping' })
    const wrapper = mount(HermesAssistantView)
    await send(wrapper)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '停止')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('已完成')
    callback!({ event: 'run.cancelled', run_id: rid, completed: false })
    done!()
    await flushPromises()
    expect(wrapper.text()).toContain('对话已停止')
    expect(wrapper.text()).toContain('停止对话不会取消已提交的业务任务')
    expect(wrapper.text()).not.toContain('已完成')
    wrapper.unmount()
  })
  it('discards corrupt local state safely', async () => {
    sessionStorage.setItem(key, JSON.stringify({ messages: [null] }))
    const wrapper = mount(HermesAssistantView)
    await flushPromises()
    expect(wrapper.text()).toContain('有什么需要一起完成？')
    wrapper.unmount()
  })
})
