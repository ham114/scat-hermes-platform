import { describe, expect, it } from 'vitest'
import { isPlotRequest } from './plotIntent'
describe('explicit plot intent', () => {
  it('recognizes a direct plot and an anaphoric followup', () => {
    expect(isPlotRequest('绘制100002的二维轨迹图')).toBe(true)
    expect(isPlotRequest('把它的二维轨迹图画出来。')).toBe(true)
    expect(isPlotRequest('有哪些模型？')).toBe(false)
    expect(isPlotRequest('不要运行预测，只画轨迹图')).toBe(true)
    expect(isPlotRequest('无需猜测脚本，请绘制轨迹图')).toBe(true)
    expect(isPlotRequest('不要画轨迹图，只分析')).toBe(false)
  })
})
