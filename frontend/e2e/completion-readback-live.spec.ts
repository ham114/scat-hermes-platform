import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
test.skip(process.env.SCAT_COMPLETION_READBACK !== '1', 'Explicit GET-only acceptance readback')
const folder = resolve(
  import.meta.dirname,
  '../../var/validation/20261107-query-display-completion',
)
for (const key of ['old_scope', 'plot', 'fresh_plot']) {
  test(
    key + ' restores verified scope and graph without another model request',
    async ({ page }, info) => {
      const report = JSON.parse(readFileSync(resolve(folder, 'natural-acceptance.json'), 'utf8'))
      const row = report.records.find((r: { case: string }) => r.case === key)
      let writes = 0
      await page.route('**/api/v1/**', (route) => {
        if (!['GET', 'HEAD'].includes(route.request().method())) {
          writes++
          return route.abort()
        }
        return route.continue()
      })
      if (key === 'old_scope') {
        await page.goto('/?thread_id=0f57051f-cd09-4497-bb79-1e66cc12f9c8')
        const reply = page.locator('.assistant-message.assistant').nth(1)
        await expect(reply).toContainText('轨迹明细已读224/524点')
        await expect(reply).not.toContainText('读取了全部524')
        await page.reload()
        await expect(reply).toContainText('轨迹明细已读224/524点')
      } else {
        const path = '/?thread_id=' + row.thread_id
        await page.goto(path)
        if (key === 'plot') {
          const reply = page.locator('.assistant-message.assistant').nth(2)
          await reply.getByRole('button', { name: /查看来源/ }).click()
          await reply.getByRole('button', { name: '查看数据', exact: true }).click()
        }
        const scene = page.locator('.cesium-container')
        const points = key === 'plot' ? '524' : '393'
        await expect(scene).toHaveAttribute('data-point-count', points, { timeout: 60000 })
        await expect(page.locator('.analysis-result')).toHaveCount(1)
        if (key === 'fresh_plot') {
          await page.reload()
          await expect(scene).toHaveAttribute('data-point-count', points, { timeout: 60000 })
        }
      }
      expect(writes).toBe(0)
      writeFileSync(
        resolve(folder, `readback-${key}-${info.project.name}.json`),
        JSON.stringify(
          {
            status: 'passed',
            writes,
            scope_corrected: key === 'old_scope',
            plot_points: key === 'old_scope' ? null : key === 'plot' ? 524 : 393,
            raw_data_saved: false,
          },
          null,
          2,
        ),
      )
    },
  )
}
