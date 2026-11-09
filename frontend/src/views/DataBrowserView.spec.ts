import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { defineComponent } from 'vue'
import DataBrowserView from './DataBrowserView.vue'
const RouterLinkStub = defineComponent({
  props: { to: { type: [String, Object], required: true } },
  template: '<a><slot /></a>',
})
describe('Data browser', () => {
  it('keeps data browsing behind an explicit route', () => {
    const wrapper = mount(DataBrowserView, { global: { stubs: { RouterLink: RouterLinkStub } } })
    expect(wrapper.text()).toContain('航班检索')
    expect(wrapper.text()).toContain('预测评估')
    expect(wrapper.find('.artifact-open-link').exists()).toBe(false)
  })
  it('preserves the result conversation when opening a saved result', async () => {
    const wrapper = mount(DataBrowserView, { global: { stubs: { RouterLink: RouterLinkStub } } })
    await wrapper
      .get('input')
      .setValue(
        'http://localhost:5174/visualizations/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/3d?thread_id=thread-1',
      )
    const link = wrapper
      .findAllComponents(RouterLinkStub)
      .find((x) => x.classes().includes('artifact-open-link'))
    expect(link?.props('to')).toEqual({
      name: 'visualization-3d',
      params: { artifactId: 'a'.repeat(32) },
      query: { thread_id: 'thread-1' },
    })
  })
})
