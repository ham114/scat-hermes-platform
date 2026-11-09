import type { InjectionKey } from 'vue'

export function isDisplayRequest(text: string): boolean {
  if (/(航班|批次|架次|数据库|数据集|检索|查询|获取)/.test(text)) return false
  return /(?:改成|改为|切换为).*(?:单列|双列|柱状图|折线图|默认图表)|(?:隐藏|显示|置顶).*(?:第?[\d一二三四五六七八九十]+.*(?:项|图表|内容))|恢复默认(?:展示|布局)/.test(
    text,
  )
}

export function displayIntent(text: string): string | null {
  const value = text
    .trim()
    .replace(/[。！!]$/, '')
    .replace(/^请/, '')
  const layout = /^(?:把|将)?(?:当前)?(?:结果|展示|布局)(?:改成|改为|切换为)(单列|双列)$/.exec(
    value,
  )
  if (layout) return layout[1]!
  const chart =
    /^(?:把|将)?(?:当前)?(?:结果的)?图表(?:改成|改为|切换为)(折线图|柱状图|默认图表)$/.exec(value)
  if (chart) return chart[1]!
  const block = /^(隐藏|显示|置顶)(?:当前结果的)?第?\s*(\d+)\s*(?:项|个内容|个内容块|个图表)$/.exec(
    value,
  )
  if (block) return `${block[1]} ${block[2]}`
  if (/^(?:恢复默认展示|恢复默认布局)$/.test(value)) return '恢复默认'
  return null
}

export interface ChatDisplay {
  register(apply: (command: string) => string): { activate(): void; dispose(): void }
  apply(command: string): string
  clear(): void
}
export const chatDisplayKey: InjectionKey<ChatDisplay> = Symbol('chat-display')

export function createChatDisplay(): ChatDisplay {
  const targets = new Map<symbol, (command: string) => string>()
  let active: symbol | undefined
  return {
    register(apply) {
      const key = Symbol()
      targets.set(key, apply)
      active = targets.size === 1 ? key : undefined
      return {
        activate() {
          if (targets.has(key)) active = key
        },
        dispose() {
          targets.delete(key)
          if (active === key) active = targets.size === 1 ? targets.keys().next().value : undefined
        },
      }
    },
    apply(command) {
      const target = active && targets.get(active)
      if (!target) throw Error('请先打开要调整的结果；有多个结果时，先点击其中一个。')
      return target(command)
    },
    clear() {
      targets.clear()
      active = undefined
    },
  }
}
