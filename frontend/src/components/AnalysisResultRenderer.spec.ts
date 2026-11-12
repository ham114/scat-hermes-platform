import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import fixture from '@/test-fixtures/analysis.json'
import { parseAnalysisArtifact } from '@/lib/analysisDisplay'
import BaseChart from './BaseChart.vue'
import AnalysisResultRenderer from './AnalysisResultRenderer.vue'
import { chatDisplayKey, createChatDisplay } from '@/lib/chatDisplay'

function render(payload = fixture) {
  return mount(AnalysisResultRenderer, {
    props: {
      result: parseAnalysisArtifact(payload, payload.artifact_id)!,
      artifactId: payload.artifact_id,
    },
    global: { stubs: { BaseChart: true } },
  })
}

describe('generic analysis rendering', () => {
  it('applies chat presentation changes to a fixed result and restores them without changing data', async () => {
    localStorage.clear()
    const bus = createChatDisplay()
    const result = parseAnalysisArtifact(fixture, fixture.artifact_id)!
    const original = JSON.stringify(result)
    const options = {
      props: { result, displayIdentity: 'chat-fixed-result:digest' },
      global: { stubs: { BaseChart: true }, provide: { [chatDisplayKey as symbol]: bus } },
    }
    const wrapper = mount(AnalysisResultRenderer, options)
    expect(bus.apply('双列')).toContain('保存')
    await flushPromises()
    expect(wrapper.find('.display-grid').classes()).toContain('dual')
    expect(JSON.stringify(result)).toBe(original)
    wrapper.unmount()
    expect(() => bus.apply('单列')).toThrow('先打开')
    const restored = mount(AnalysisResultRenderer, options)
    expect(restored.find('.display-grid').classes()).toContain('dual')
    restored.unmount()
    localStorage.clear()
  })
  it('binds saved composite layouts to every source digest', async () => {
    localStorage.clear()
    const wrapper = mount(AnalysisResultRenderer, {
      props: {
        result: parseAnalysisArtifact(fixture, fixture.artifact_id)!,
        displayIdentity: 'first:digest1;second:digest2',
      },
      global: { stubs: { BaseChart: true } },
    })
    await wrapper.get('input[aria-label="展示调整"]').setValue('双列')
    await wrapper.get('form').trigger('submit')
    await wrapper.get('button[type="button"]').trigger('click')
    expect(
      localStorage.getItem('hermes-display-receipts-v1:first:digest1;second:digest2'),
    ).toContain('"columns":2')
    await wrapper.setProps({ displayIdentity: 'first:digest1;second:changed' })
    expect(wrapper.get('.display-grid').classes()).not.toContain('dual')
    wrapper.unmount()
    localStorage.clear()
  })
  it('shows seven types, source references, units and classification confidence', () => {
    const wrapper = render()
    expect(wrapper.findAll('[data-block-type]')).toHaveLength(7)
    expect(wrapper.text()).toContain('12.75（m）')
    expect(wrapper.text()).toContain('未提供（m）')
    expect(wrapper.text()).toContain('误差（m）')
    expect(wrapper.text()).toContain('0.9')
    expect(wrapper.text()).toContain(fixture.artifact_id)
    expect(wrapper.findAllComponents(BaseChart)).toHaveLength(4)
    expect(wrapper.get('a').attributes('href')).toBe('/assistant')
    wrapper.unmount()
  })

  it('sorts the current table from its header without changing the saved result', async () => {
    const payload = structuredClone(fixture)
    const table = payload.analysis_result.blocks.find((block) => block.type === 'table')!
    table.rows!.push({ sample: 'smaller', error: 2 })
    const wrapper = render(payload)
    const result = wrapper.get('[data-block-type="table"]')
    const header = result.findAll('th').at(-1)!
    await header.get('button').trigger('click')
    expect(header.attributes('aria-sort')).toBe('ascending')
    expect(result.findAll('tbody tr').at(0)!.text()).toContain('smaller')
    await header.get('button').trigger('click')
    expect(header.attributes('aria-sort')).toBe('descending')
    expect(result.findAll('tbody tr').at(0)!.text()).toContain('SYNTH-H6-001')
    expect(wrapper.props('result').blocks[1]).toEqual(table)
    wrapper.unmount()
  })
  it('updates snapshot series without fetching or recomputing an analysis', async () => {
    const wrapper = render()
    await wrapper.get('select').setValue('时刻B')
    await flushPromises()
    const chart = wrapper
      .findAllComponents(BaseChart)
      .find((item) => item.props('label') === '观测与预测轨迹')!
    expect((chart.props('option') as { series: unknown[] }).series).toHaveLength(1)
    wrapper.unmount()
  })
  it('escapes titles and disables unsafe action links', () => {
    const payload = structuredClone(fixture)
    payload.analysis_result.title = '<img src=x onerror=alert(1)>'
    payload.analysis_result.blocks.at(-1)!.url = 'javascript:alert(1)'
    const wrapper = render(payload)
    expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).toContain('链接不符合展示规则')
    wrapper.unmount()
  })
  it('represents empty result sets without invented chart values', () => {
    const payload = structuredClone(fixture)
    for (const block of payload.analysis_result.blocks) {
      if ('points' in block) block.points = []
      if ('rows' in block) block.rows = []
      if (block.type === 'classification') block.items = []
    }
    const wrapper = render(payload)
    expect(wrapper.findAllComponents(BaseChart)).toHaveLength(0)
    expect(wrapper.text()).toContain('没有可展示')
    wrapper.unmount()
  })
})
