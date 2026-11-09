import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.skip(
  process.env.SCAT_CLOSEOUT_DISPLAY !== '1',
  'Explicit read-only reuse of successful GPU result',
)

test('chat changes a saved result layout, refresh restores it, and 3D remains visible without writes', async ({
  page,
}, info) => {
  const record = JSON.parse(
    readFileSync(
      resolve(
        import.meta.dirname,
        '../../var/validation/20261108-platform-closeout/natural-acceptance.json',
      ),
      'utf8',
    ),
  )
  if (record.status !== 'passed') throw Error('Successful natural case required')
  let writes = 0,
    auth = 0
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.name))
  page.on('request', (r) => {
    if (r.headers().authorization) auth++
  })
  await page.route('**/api/v1/**', (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      writes++
      return route.abort()
    }
    return route.continue()
  })
  await page.goto('/?thread_id=' + record.thread_id)
  const card = page.locator('.task-card').filter({ hasText: record.task_id })
  await card.getByRole('button', { name: /^查看预测轨迹/ }).click()
  const panel = card.getByRole('region', { name: '结构化分析结果' })
  await panel.locator('header').first().click()
  await page.getByLabel('你的问题', { exact: true }).fill('请将当前结果改成双列。')
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(panel.locator('.display-grid')).toHaveClass(/dual/)
  await expect(panel.getByRole('status').filter({ hasText: '展示已调整并保存' })).toBeVisible()
  await page.reload()
  await card.getByRole('button', { name: /^查看预测轨迹/ }).click()
  await expect(panel.locator('.display-grid')).toHaveClass(/dual/)
  const scene = panel.locator('.cesium-container').first()
  await expect(scene).toHaveAttribute('data-point-count', '40', { timeout: 60000 })
  await panel.getByRole('button', { name: '打开轨迹交互' }).click()
  const workspace = panel.getByRole('region', { name: '轨迹交互工作台', exact: true })
  await workspace.getByRole('button', { name: '切换三维' }).click()
  await expect(workspace.locator('.cesium-container')).toHaveAttribute('data-scene-dimension', '3d')
  await page.getByLabel('你的问题', { exact: true }).fill('把第二张图改成柱状图')
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(page.getByRole('alert').filter({ hasText: '当前支持' })).toBeVisible()
  expect(writes).toBe(0)
  expect(auth).toBe(0)
  expect(errors).toEqual([])
  writeFileSync(
    resolve(
      import.meta.dirname,
      `../../var/validation/20261108-platform-closeout/display-${info.project.name}.json`,
    ),
    JSON.stringify(
      {
        status: 'passed',
        writes,
        auth,
        page_errors: errors,
        graph_points: 40,
        chat_layout: true,
        refresh_restored: true,
        new_gpu_calls: 0,
      },
      null,
      2,
    ),
  )
})
