/** Only an explicit positive plotting request opts into automatic display reads. */
export function isPlotRequest(text: string): boolean {
  if (/(?:不要|不必|无需|不用|别再?|不需要).{0,3}(?:画|绘制|出图)/.test(text)) return false
  return /(?:绘制|画出|画一|画下|画个|画.*出来).{0,24}(?:轨迹|航迹)|(?:轨迹|航迹).{0,24}(?:图|画|绘制)/.test(
    text,
  )
}
