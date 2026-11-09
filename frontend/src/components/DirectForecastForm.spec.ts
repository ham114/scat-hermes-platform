import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import DirectForecastForm from './DirectForecastForm.vue'
const models = [
  {
    model_id: 'scat-gbar-v23',
    version: '23',
    execution: 'gpu',
    constraints: {
      min_observed_s: 350,
      max_observation_points: 500,
      max_horizon_s: 360,
      forecast_horizon_multiple_s: 10,
    },
  },
]
async function fill(wrapper: ReturnType<typeof mount>) {
  await wrapper.get('[aria-label="数据批次"]').setValue('batch')
  await wrapper.get('[aria-label="航班编号"]').setValue('flight')
  await wrapper.get('select').setValue('scat-gbar-v23')
}
describe('direct forecast preparation', () => {
  it('emits a versioned point-exact future proposal and reduced budget without confirming it', async () => {
    const wrapper = mount(DirectForecastForm, { props: { models } })
    await fill(wrapper)
    await wrapper.get('[aria-label="保守计量上限"]').setValue(430041)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('prepare')).toEqual([
      [
        expect.objectContaining({
          kind: 'forecast',
          models: [{ model: 'scat-gbar-v23', version: '23' }],
          min_observed_s: 350,
          observation_last_n: 200,
          forecast_origin: 'last_observation',
          horizons_s: [10, 30, 60],
        }),
        { max_llm_tokens: 430041 },
      ],
    ])
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })
  it.each([
    ['最后观测点数', 501],
    ['未来时距（秒）', '11,30'],
    ['未来时距（秒）', '30,10'],
    ['保守计量上限', 1000001],
  ])('rejects invalid %s before sending', async (label, value) => {
    const wrapper = mount(DirectForecastForm, { props: { models } })
    await fill(wrapper)
    await wrapper.get(`[aria-label="${label}"]`).setValue(value)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('prepare')).toBeUndefined()
    expect(wrapper.get('[role="alert"]').text()).not.toBe('')
  })
  it('blocks preparation in a readonly session even when invoked programmatically', async () => {
    const wrapper = mount(DirectForecastForm, { props: { models, readOnly: true } })
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('prepare')).toBeUndefined()
  })
})
