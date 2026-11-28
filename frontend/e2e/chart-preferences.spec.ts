import { expect, test } from '@playwright/test'
import { join } from 'node:path'

test('chart preferences can be corrected, paused, individually deleted and cleared', async ({
  page,
}, info) => {
  let epoch = 1,
    revision = 0,
    enabled = true,
    deletion = 'none'
  let rows: {
    id: string
    key: string
    value: string | number
    task_type: string
    evidence: string
    confidence: string
    source_artifact: null
    updated: number
    supersedes: null
  }[] = []
  let writes = 0,
    clears = 0
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('console', (m) => {
    if (['error', 'warning'].includes(m.type())) errors.push(m.text())
  })
  const preferences = () => ({ enabled, epoch, revision, items: rows })
  await page.route('**/portal-session', (route) =>
    route.fulfill({ json: { username: 'qa', owner_id: 'preference-qa', role: 'user' } }),
  )
  await page.route('**/api/v1/hermes/memory**', (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname
    if (path.endsWith('/chart-preferences')) {
      if (request.method() === 'POST') {
        const body = request.postDataJSON()
        expect(body.epoch).toBe(epoch)
        expect(body.revision).toBe(revision)
        writes++
        revision++
        rows = rows.filter((r) => r.key !== body.key || r.task_type !== body.task_type)
        if (body.value !== null)
          rows.push({
            id: String(writes),
            key: body.key,
            value: body.value,
            task_type: body.task_type,
            evidence: '用户在记忆页明确设置',
            confidence: 'explicit',
            source_artifact: null,
            updated: 1,
            supersedes: null,
          })
      }
      return route.fulfill({ json: preferences() })
    }
    if (path.endsWith('/corrections')) return route.fulfill({ json: { items: [] } })
    if (path.endsWith('/settings')) {
      enabled = request.postDataJSON().enabled
      revision++
      return route.fulfill({ json: { enabled } })
    }
    if (request.method() === 'DELETE') {
      clears++
      enabled = false
      epoch++
      revision++
      rows = []
      deletion = 'pending'
      return route.fulfill({
        status: 202,
        json: { enabled, deletion, chat_history_deleted: false },
      })
    }
    return route.fulfill({
      json: { enabled, epoch, chart_revision: revision, queue: {}, deletion },
    })
  })
  await page.goto('/memory')
  await expect(page).toHaveURL(/\/memory$/)
  await expect(page).toHaveTitle('记忆 · Hermes')
  const panel = page.getByLabel('图表长期偏好')
  await expect(panel.getByText('尚无已保存的图表偏好。')).toBeVisible()
  await panel.getByLabel('图表偏好值').fill('蓝色系')
  await panel.getByRole('button', { name: '保存图表偏好', exact: true }).click()
  await expect(panel.getByText('所有图表 · 配色：蓝色系', { exact: true })).toBeVisible()
  await panel.getByRole('button', { name: '更正配色', exact: true }).click()
  await panel.getByLabel('图表偏好值').fill('色盲友好配色')
  await panel.getByRole('button', { name: '保存图表偏好', exact: true }).click()
  await expect(panel.getByText('所有图表 · 配色：色盲友好配色', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '暂停记忆', exact: true }).click()
  await expect(
    panel.getByText('记忆已暂停：保存的偏好不会用于新图，也不会记录新偏好。'),
  ).toBeVisible()
  await expect(panel.getByRole('button', { name: '保存图表偏好', exact: true })).toBeDisabled()
  await panel.getByRole('button', { name: '删除配色偏好', exact: true }).click()
  await expect(panel.getByText('尚无已保存的图表偏好。')).toBeVisible()
  await page.getByRole('button', { name: '启用记忆', exact: true }).click()
  await panel.getByLabel('图表偏好项目').selectOption('font_size')
  await panel.getByLabel('图表偏好值').fill('18')
  await panel.getByRole('button', { name: '保存图表偏好', exact: true }).click()
  await expect(panel.getByText('所有图表 · 字号：18', { exact: true })).toBeVisible()
  expect(writes).toBe(4)
  await panel.getByLabel('图表偏好项目').selectOption('altitude_unit')
  await panel.getByLabel('图表偏好值').fill('米')
  await panel.getByRole('button', { name: '保存图表偏好', exact: true }).click()
  await expect(panel.getByText('所有图表 · 高度单位：米', { exact: true })).toBeVisible()
  if (process.env.SCAT_VIS_QA_DIR) {
    await panel.screenshot({
      path: join(process.env.SCAT_VIS_QA_DIR, `p5-preferences-${info.project.name}.png`),
    })
  }
  await page.getByLabel('确认清空当前用户的长期记忆').check()
  await page.getByRole('button', { name: '清空记忆', exact: true }).click()
  await expect(panel.getByText('尚无已保存的图表偏好。')).toBeVisible()
  await expect(page.getByText('记忆已暂停', { exact: true })).toBeVisible()
  await expect(panel.getByText('所有图表 · 字号：18', { exact: true })).toHaveCount(0)
  await page.reload()
  await expect(page.getByLabel('图表长期偏好').getByText('尚无已保存的图表偏好。')).toBeVisible()
  expect(clears).toBe(1)
  expect(writes).toBe(5)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
  expect(errors).toEqual([])
})
