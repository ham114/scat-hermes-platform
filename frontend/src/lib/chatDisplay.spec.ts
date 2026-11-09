import { describe, expect, it } from 'vitest'
import { createChatDisplay, displayIntent, isDisplayRequest } from './chatDisplay'

describe('chat display targets', () => {
  it('recognizes presentation only and leaves data/model requests for the normal flow', () => {
    expect(displayIntent('请将当前结果改成双列。')).toBe('双列')
    expect(displayIntent('把图表改成柱状图')).toBe('柱状图')
    expect(displayIntent('隐藏第2项')).toBe('隐藏 2')
    expect(displayIntent('绘制它的二维轨迹图')).toBeNull()
    expect(displayIntent('将结果改成双列并预测未来60秒')).toBeNull()
    expect(displayIntent('把第二张图改成柱状图')).toBeNull()
    expect(isDisplayRequest('把第二张图改成柱状图')).toBe(true)
    expect(isDisplayRequest('绘制它的二维轨迹图')).toBe(false)
    expect(isDisplayRequest('显示第2个航班的图表')).toBe(false)
    expect(isDisplayRequest('查询第二个架次并显示图表')).toBe(false)
  })
  it('requires a selected result when multiple surfaces exist and forgets old scopes', () => {
    const bus = createChatDisplay()
    const first = bus.register((c) => 'first:' + c)
    expect(bus.apply('双列')).toBe('first:双列')
    const second = bus.register((c) => 'second:' + c)
    expect(() => bus.apply('双列')).toThrow('先点击')
    first.activate()
    expect(bus.apply('双列')).toBe('first:双列')
    bus.clear()
    first.activate()
    second.activate()
    expect(() => bus.apply('双列')).toThrow('先打开')
    first.dispose()
    second.dispose()
  })
})
