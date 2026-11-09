import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PortalView from './PortalView.vue'
describe('PortalView', () => {
  it('opens the conversation directly without a dataset landing page', () => {
    const wrapper = mount(PortalView, {
      global: { stubs: { HermesAssistantView: { template: '<section>对话工作台</section>' } } },
    })
    expect(wrapper.text()).toBe('对话工作台')
    expect(wrapper.text()).not.toMatch(/SCAT|UAV|批次|MongoDB|数据集/)
    expect(wrapper.find('a[href]').exists()).toBe(false)
  })
})
