import { expect, test } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
test.skip(
  process.env.SCAT_RESULT_SUMMARY_LIVE !== '1',
  'Explicit GET-only existing receipt validation',
)
for (const sample of [
  {
    name: 'three overlapping sampled reads',
    thread: '3a298bec-bafd-48bd-b99c-509f295dbd1f',
    turn: 1,
    points: '108',
    sources: 3,
    note: '去重1点；已加载108/524点',
  },
  {
    name: 'six complete pages plus overview',
    thread: '3a298bec-bafd-48bd-b99c-509f295dbd1f',
    turn: 0,
    points: '524',
    sources: 7,
    note: '已覆盖完整源序列',
  },
  {
    name: 'stopped request keeps overview and one track page',
    thread: 'd450ce8d-b3b2-4cd7-aad6-f787f26954c4',
    turn: 2,
    points: '100',
    sources: 2,
    note: '已加载100/393点',
  },
]) {
  test(sample.name, async ({ page }, info) => {
    let writes = 0
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route('**/api/v1/**', (route) => {
      if (!['GET', 'HEAD'].includes(route.request().method())) {
        writes++
        return route.abort()
      }
      return route.continue()
    })
    await page.goto('/?thread_id=' + sample.thread)
    await page
      .getByRole('button', { name: /查看来源/ })
      .nth(sample.turn)
      .click()
    const panel = page.getByRole('region', { name: '结果来源', exact: true })
    await expect(
      panel.getByText(`本轮 ${sample.sources} 份数据来源，用于同一次绘图或分析。`),
    ).toBeVisible()
    await panel.getByRole('button', { name: '查看数据', exact: true }).click()
    await expect(panel.getByRole('region', { name: '本轮主结果' })).toHaveCount(1)
    await expect(panel.locator('.analysis-result')).toHaveCount(1)
    await expect(panel.locator('.cesium-container')).toHaveAttribute(
      'data-point-count',
      sample.points,
    )
    await expect(panel.getByText(sample.note, { exact: false })).toBeVisible()
    await expect(
      panel.getByText('本次指令未完整完成，以下仅为已取得的数据，不代表分析已完成。'),
    ).toBeVisible()
    const details = panel.locator('.source-details')
    expect(await details.getAttribute('open')).toBeNull()
    await details.locator('summary').first().click()
    await expect(details.locator('article')).toHaveCount(sample.sources)
    expect(writes).toBe(0)
    expect(errors).toEqual([])
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const folder = resolve(import.meta.dirname, '../../var/validation/20261107-result-summary')
    mkdirSync(folder, { recursive: true })
    writeFileSync(
      resolve(folder, `live-${sample.points}-${info.project.name}.json`),
      JSON.stringify(
        {
          status: 'passed',
          displayed_points: Number(sample.points),
          sources: sample.sources,
          main_results: 1,
          source_details_collapsed: true,
          partial_run_notice: true,
          writes,
          errors,
          raw_data_saved: false,
        },
        null,
        2,
      ),
    )
  })
}
