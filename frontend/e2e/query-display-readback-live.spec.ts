import { expect, test } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
test.skip(process.env.SCAT_QUERY_READBACK_LIVE !== '1', 'Explicit GET-only live readback')
const folder = resolve(import.meta.dirname, '../../var/validation/20261107-query-display-rounds')
for (const item of [
  { key: 'flight', turn: 1, points: '224' },
  { key: 'followup_plot', turn: 2, points: '100' },
  { key: 'fresh_plot', turn: 0, points: '100' },
]) {
  test(
    item.key + ' preserves sources and refreshes without re-execution',
    async ({ page }, info) => {
      const report = JSON.parse(readFileSync(resolve(folder, 'natural-rounds.json'), 'utf8'))
      const row = report.records.find((r: { case: string }) => r.case === item.key)
      let writes = 0
      await page.route('**/api/v1/**', (route) => {
        if (!['GET', 'HEAD'].includes(route.request().method())) {
          writes++
          return route.abort()
        }
        return route.continue()
      })
      const open = async () => {
        await page.goto('/?thread_id=' + row.thread_id)
        await page
          .getByRole('button', { name: /查看来源/ })
          .nth(item.turn)
          .click()
        const panel = page.getByRole('region', { name: '结果来源', exact: true })
        await panel.getByRole('button', { name: '查看数据', exact: true }).click()
        await expect(panel.locator('.cesium-container')).toHaveAttribute(
          'data-point-count',
          item.points,
        )
        await expect(panel.getByRole('region', { name: '本轮主结果' })).toHaveCount(1)
        await expect(panel.locator('.source-details')).toHaveCount(1)
      }
      await open()
      await page.reload()
      await open()
      expect(writes).toBe(0)
      writeFileSync(
        resolve(folder, `readback-${item.key}-${info.project.name}.json`),
        JSON.stringify(
          {
            status: 'passed',
            run_id: row.run_id,
            points: Number(item.points),
            single_main_result: true,
            refresh_restores: true,
            writes,
            raw_data_saved: false,
          },
          null,
          2,
        ),
      )
    },
  )
}
