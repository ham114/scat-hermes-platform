import ElementPlus from 'element-plus'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { ScatApiError } from '@/api/errors'
import AsyncStatePanel from './AsyncStatePanel.vue'

describe('AsyncStatePanel', () => {
  it('shows a traceable request id and emits retry', async () => {
    const wrapper = mount(AsyncStatePanel, {
      props: {
        state: 'error',
        error: new ScatApiError({
          kind: 'http',
          status: 503,
          message: '服务暂不可用',
          requestId: 'req-123',
        }),
      },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).toContain('服务暂不可用')
    expect(wrapper.text()).toContain('req-123')
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('renders the empty state copy', () => {
    const wrapper = mount(AsyncStatePanel, {
      props: { state: 'empty', emptyDescription: '没有可用批次' },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).toContain('没有可用批次')
  })
})
