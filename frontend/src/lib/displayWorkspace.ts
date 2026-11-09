import type { AnalysisResult, AnalysisBlock } from './analysisDisplay'
export interface DisplayConfig {
  version: 1
  columns: 1 | 2
  order: number[]
  hidden: number[]
  chart: 'default' | 'line' | 'bar'
  sort: Record<string, { key: string; descending: boolean }>
  fields: Record<string, string[]>
}
export const defaultDisplay = (result: AnalysisResult): DisplayConfig => ({
  version: 1,
  columns: 1,
  order: result.blocks.map((_, i) => i),
  hidden: [],
  chart: 'default',
  sort: {},
  fields: {},
})
export function parseDisplay(value: unknown, result: AnalysisResult): DisplayConfig {
  const c = value as DisplayConfig
  if (
    !c ||
    c.version !== 1 ||
    ![1, 2].includes(c.columns) ||
    !['default', 'line', 'bar'].includes(c.chart) ||
    !Array.isArray(c.order) ||
    c.order.length !== result.blocks.length ||
    new Set(c.order).size !== c.order.length ||
    c.order.some((i) => !Number.isInteger(i) || i < 0 || i >= result.blocks.length) ||
    !Array.isArray(c.hidden) ||
    c.hidden.some((i) => !c.order.includes(i)) ||
    !c.sort ||
    !c.fields
  )
    throw Error('展示配置无效')
  for (const [index, sort] of Object.entries(c.sort)) {
    const block = result.blocks[Number(index)]
    if (
      block?.type !== 'table' ||
      !block.columns.some((x) => x.key === sort.key) ||
      typeof sort.descending !== 'boolean'
    )
      throw Error('排序字段无效')
  }
  for (const [index, fields] of Object.entries(c.fields)) {
    const block = result.blocks[Number(index)]
    if (
      block?.type !== 'table' ||
      !Array.isArray(fields) ||
      !fields.length ||
      fields.some((k) => !block.columns.some((x) => x.key === k))
    )
      throw Error('展示列无效')
  }
  return JSON.parse(JSON.stringify(c)) as DisplayConfig
}
/** Deliberately bounded presentation language: never turns analysis requests into computations. */
export function displayCommand(
  text: string,
  current: DisplayConfig,
  result: AnalysisResult,
): DisplayConfig {
  const next = JSON.parse(JSON.stringify(current)) as DisplayConfig
  const commands = text
    .trim()
    .split(/[，,；;。]/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (!commands.length) throw Error('请输入展示调整')
  for (const command of commands) {
    if (/^(单列|双列)$/.test(command)) next.columns = command === '单列' ? 1 : 2
    else if (/^(折线图|柱状图|默认图表)$/.test(command))
      next.chart = command === '折线图' ? 'line' : command === '柱状图' ? 'bar' : 'default'
    else if (command === '恢复默认') Object.assign(next, defaultDisplay(result))
    else {
      const match = /^(隐藏|显示|置顶|升序|降序|只看)\s*(\d+)(?:\s+(.+))?$/.exec(command)
      if (!match) throw Error('支持：双列；置顶 2；降序 2 字段；只看 2 字段1 字段2；隐藏 3；柱状图')
      const index = Number(match[2]) - 1
      const block = result.blocks[index]
      if (!block) throw Error('内容块编号不存在')
      if (match[1] === '隐藏') next.hidden = [...new Set([...next.hidden, index])]
      if (match[1] === '显示') next.hidden = next.hidden.filter((i) => i !== index)
      if (match[1] === '置顶') next.order = [index, ...next.order.filter((i) => i !== index)]
      if (['升序', '降序', '只看'].includes(match[1]!)) {
        if (block.type !== 'table' || !match[3]) throw Error('请选择表格与字段')
        const keys = match[3]
          .split(/\s+/)
          .map((k) => block.columns.find((x) => x.key === k || x.label === k)?.key)
        if (keys.some((k) => !k)) throw Error('表格中没有该字段')
        if (match[1] === '只看') next.fields[index] = keys as string[]
        else {
          if (keys.length !== 1) throw Error('每次选择一个排序字段')
          next.sort[index] = { key: keys[0]!, descending: match[1] === '降序' }
        }
      }
    }
  }
  return parseDisplay(next, result)
}
export function displayBlocks(
  result: AnalysisResult,
  c: DisplayConfig,
): { id: number; block: AnalysisBlock }[] {
  return c.order
    .filter((i) => !c.hidden.includes(i))
    .map((id) => {
      const block = JSON.parse(JSON.stringify(result.blocks[id]!)) as AnalysisBlock
      if (block.type === 'table') {
        const sort = c.sort[id]
        if (sort)
          block.rows.sort((a, b) => {
            const x = a[sort.key],
              y = b[sort.key]
            if (x == null) return y == null ? 0 : 1
            if (y == null) return -1
            return (
              (typeof x === 'number' && typeof y === 'number'
                ? x - y
                : String(x).localeCompare(String(y))) * (sort.descending ? -1 : 1)
            )
          })
        if (c.fields[id]) block.columns = block.columns.filter((x) => c.fields[id]!.includes(x.key))
      }
      return { id, block }
    })
}
