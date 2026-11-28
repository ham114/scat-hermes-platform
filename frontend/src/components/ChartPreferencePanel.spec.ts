import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { memoryRequest } from '@/api/memory'
import ChartPreferencePanel from './ChartPreferencePanel.vue'

vi.mock('@/api/memory', () => ({ memoryRequest: vi.fn() }))
const initial = { enabled: true, epoch: 1, revision: 0, items: [] }
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(memoryRequest).mockResolvedValue(initial)
})

it('accepts a natural-language altitude default without a separate scope selector', async () => {
  const wrapper = mount(ChartPreferencePanel, { props: { epoch: 1, enabled: true } })
  await flushPromises()
  await wrapper.get('[aria-label="图表偏好项目"]').setValue('altitude_unit')
  await wrapper.get('input').setValue('未说明单位时默认以米显示')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(vi.mocked(memoryRequest).mock.calls.find((call) => call[1] === 'POST')?.[2]).toMatchObject(
    { key: 'altitude_unit', value: '未说明单位时默认以米显示' },
  )
  wrapper.unmount()
})

it('displays learned height units and keeps new semantic rules editable', async () => {
  vi.mocked(memoryRequest).mockResolvedValue({
    ...initial,
    revision: 1,
    items: [
      { id: 'height', task_type: 'all', key: 'altitude_unit', value: 'm' },
      {
        id: 'rotation',
        task_type: 'all',
        key: 'rule_long_label_rotation',
        value: '长标签倾斜45度',
      },
    ],
  })
  const wrapper = mount(ChartPreferencePanel, { props: { epoch: 1, enabled: true } })
  await flushPromises()
  expect(wrapper.text()).toContain('所有图表 · 高度单位：米')
  expect(wrapper.text()).toContain('所有图表 · 其他绘图规则：长标签倾斜45度')
  await wrapper.get('[aria-label="图表偏好项目"]').setValue('altitude_unit')
  await wrapper.get('input').setValue('英尺')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(vi.mocked(memoryRequest).mock.calls.find((call) => call[1] === 'POST')?.[2]).toMatchObject(
    { key: 'altitude_unit', value: 'ft' },
  )
  wrapper.unmount()
})

it('ignores a late save response after all memory was cleared', async () => {
  let release!: (value: unknown) => void
  vi.mocked(memoryRequest).mockImplementation(async (_path, method) => {
    if (method === 'POST')
      return new Promise((resolve) => {
        release = resolve
      }) as never
    return initial as never
  })
  const wrapper = mount(ChartPreferencePanel, { props: { epoch: 1, enabled: true, revision: 0 } })
  await flushPromises()
  await wrapper.get('input').setValue('蓝色系')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  vi.mocked(memoryRequest).mockResolvedValue({ enabled: false, epoch: 2, revision: 2, items: [] })
  await wrapper.setProps({ epoch: 2, enabled: false, revision: 2 })
  await flushPromises()
  release({
    enabled: true,
    epoch: 1,
    revision: 1,
    items: [{ id: 'old', task_type: 'all', key: 'palette', value: '蓝色系' }],
  })
  await flushPromises()
  expect(wrapper.text()).not.toContain('所有图表 · 配色：蓝色系')
  expect(wrapper.text()).not.toContain('图表偏好已在本地保存')
  expect(wrapper.text()).toContain('尚无已保存的图表偏好')
  expect((wrapper.get('input').element as HTMLInputElement).value).toBe('')
  wrapper.unmount()
})

it('reuses the same request after an uncertain edit', async () => {
  const sent: unknown[] = []
  vi.mocked(memoryRequest).mockImplementation(async (_path, method, body) => {
    if (method === 'POST') {
      sent.push(body)
      if (sent.length === 1) throw Error('unknown')
      return { ...initial, revision: 1 } as never
    }
    return initial as never
  })
  const wrapper = mount(ChartPreferencePanel, { props: { epoch: 1, enabled: true, revision: 0 } })
  await flushPromises()
  await wrapper.get('input').setValue('蓝色系')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(sent).toHaveLength(1)
  await wrapper
    .findAll('button')
    .find((button) => button.text() === '恢复原偏好请求')!
    .trigger('click')
  await flushPromises()
  expect(sent).toHaveLength(2)
  expect(sent[1]).toEqual(sent[0])
  wrapper.unmount()
})
