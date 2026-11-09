import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/api/workspace'
import HistoryView from './HistoryView.vue'
vi.mock('@/api/workspace', () => ({
  listConversationThreads: vi.fn(),
  loadConversationPage: vi.fn(),
  renameHistoryThread: vi.fn(),
}))
const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55'
const row = {
  id: 2,
  request_id: tid,
  run_id: 'run_one',
  input: '真实问题',
  input_available: true,
  output: '真实回复',
  status: 'completed',
  created_at: 1,
  updated_at: 2,
  last_sequence: 1,
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.listConversationThreads).mockResolvedValue({
    items: [{ thread_id: tid, created_at: 1, title: '会话标题', title_revision: 1 }],
    next_cursor: 'more',
  })
  vi.mocked(api.loadConversationPage).mockResolvedValue({ items: [row], next_cursor: 'older' })
})
describe('Complete conversation history', () => {
  it('shows question/reply, prepends older turns, and renames with revision', async () => {
    const wrapper = mount(HistoryView, {
      global: { stubs: { RouterLink: { template: '<a><slot/></a>' } } },
    })
    await flushPromises()
    await wrapper.get('.history-item').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('真实问题')
    expect(wrapper.text()).toContain('真实回复')
    vi.mocked(api.loadConversationPage).mockResolvedValue({
      items: [{ ...row, id: 1, input: null, input_available: false, output: '更早回复' }],
      next_cursor: null,
    })
    await wrapper
      .findAll('button')
      .find((x) => x.text() === '加载更早记录')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.history-reply')).toHaveLength(2)
    expect(wrapper.text()).toContain('提问原文未保存')
    await wrapper
      .findAll('button')
      .find((x) => x.text() === '修改标题')!
      .trigger('click')
    await wrapper.get('input').setValue('新标题')
    vi.mocked(api.renameHistoryThread).mockResolvedValue({
      thread_id: tid,
      title: '新标题',
      title_revision: 2,
    })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.renameHistoryThread).toHaveBeenCalledWith(tid, '新标题', 1)
    expect(wrapper.text()).toContain('新标题')
    wrapper.unmount()
  })
  it('drops a late old thread response after another thread is selected', async () => {
    const other = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    vi.mocked(api.listConversationThreads).mockResolvedValue({
      items: [
        { thread_id: tid, created_at: 1 },
        { thread_id: other, created_at: 2 },
      ],
      next_cursor: null,
    })
    let resolve!: (page: api.HistoryPage<api.HistoryTurn>) => void
    vi.mocked(api.loadConversationPage)
      .mockImplementationOnce(
        () =>
          new Promise((done) => {
            resolve = done
          }),
      )
      .mockResolvedValue({ items: [{ ...row, input: '新会话问题' }], next_cursor: null })
    const wrapper = mount(HistoryView, { global: { stubs: { RouterLink: true } } })
    await flushPromises()
    await wrapper.findAll('.history-item')[0]!.trigger('click')
    await wrapper.findAll('.history-item')[1]!.trigger('click')
    await flushPromises()
    resolve({ items: [{ ...row, input: '不能显示旧内容' }], next_cursor: null })
    await flushPromises()
    expect(wrapper.text()).toContain('新会话问题')
    expect(wrapper.text()).not.toContain('不能显示旧内容')
    wrapper.unmount()
  })
})
