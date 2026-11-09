import { expect, test, type Route } from '@playwright/test'
import { resolve } from 'node:path'

const tid = '3d305763-d908-45c4-9692-c994a3cb5372'
const rid = 'run_browser'
const frame = (event: string, sequence: number, fields: object = {}) =>
  `id: ${sequence}\ndata: ${JSON.stringify({ event, run_id: rid, ...fields })}\n\n`

test('assistant sends, displays tools and escaped output, and fits the viewport', async ({
  page,
}, info) => {
  let posts = 0
  await page.route('**/api/v1/hermes/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    if (path.endsWith('/threads')) return route.fulfill({ status: 201, json: { thread_id: tid } })
    if (path.endsWith('/runs')) {
      posts++
      return route.fulfill({ status: 202, json: { run_id: rid, status: 'started' } })
    }
    if (path.endsWith('/events'))
      return route.fulfill({
        contentType: 'text/event-stream',
        body:
          frame('tool.started', 0, { tool: 'mcp__scat__search_scat_flights' }) +
          frame('tool.completed', 1, { tool: 'mcp__scat__search_scat_flights', error: false }) +
          frame('message.delta', 2, { delta: '查询中' }) +
          frame('run.completed', 3, {
            completed: true,
            output: '验证样例：该批次共有 10,637 个航班。<script>window.invalid=1</script>',
          }),
      })
    return route.fulfill({ json: { run_id: rid, status: 'running' } })
  })
  await page.goto('/assistant')
  await page.getByLabel('你的问题').fill('查询批次 scat20161015_20161021 的航班数。')
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  await page.getByText('执行过程 · 1 步', { exact: true }).click()
  await expect(page.getByText('航班检索 · 完成', { exact: true })).toBeVisible()
  expect(posts).toBe(1)
  expect(await page.evaluate(() => Object.hasOwn(window, 'invalid'))).toBe(false)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/${process.env.SCAT_E2E_SCREENSHOT_PREFIX ?? '20261022-H34-assistant'}-${info.project.name}.png`,
    ),
    fullPage: true,
  })
})

test('reload reconnects the same run and cursor without another POST', async ({ page }) => {
  let posts = 0
  let streams = 0
  let resumedCursor: string | undefined
  await page.route('**/api/v1/hermes/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    if (path.endsWith('/threads')) return route.fulfill({ status: 201, json: { thread_id: tid } })
    if (path.endsWith('/runs')) {
      posts++
      return route.fulfill({ status: 202, json: { run_id: rid, status: 'started' } })
    }
    if (path.endsWith('/events')) {
      streams++
      if (streams === 1)
        return route.fulfill({
          contentType: 'text/event-stream',
          body: frame('message.delta', 2, { delta: '部分回答' }),
        })
      resumedCursor = route.request().headers()['last-event-id']
      return route.fulfill({
        contentType: 'text/event-stream',
        body: frame('run.completed', 3, { completed: true, output: '恢复后的完整结果' }),
      })
    }
    return route.fulfill({ json: { run_id: rid, status: 'running' } })
  })
  await page.goto('/assistant')
  await page.getByLabel('你的问题').fill('检索文档')
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect(page.getByRole('button', { name: '恢复本次请求' })).toBeVisible()
  await page.reload()
  await expect(page.getByText('恢复后的完整结果', { exact: true })).toBeVisible()
  expect(resumedCursor).toBe('2')
  expect(posts).toBe(1)
})

test('stop displays the actual cancelled state', async ({ page }) => {
  let stream: Route | undefined
  await page.route('**/api/v1/hermes/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    if (path.endsWith('/threads')) return route.fulfill({ status: 201, json: { thread_id: tid } })
    if (path.endsWith('/runs'))
      return route.fulfill({ status: 202, json: { run_id: rid, status: 'started' } })
    if (path.endsWith('/events')) {
      stream = route
      return
    }
    if (path.endsWith('/stop')) {
      await stream!.fulfill({
        contentType: 'text/event-stream',
        body: frame('run.cancelled', 0, { completed: false, interrupted: true }),
      })
      return route.fulfill({ json: { run_id: rid, status: 'stopping' } })
    }
    return route.fulfill({ json: { run_id: rid, status: 'running' } })
  })
  await page.goto('/assistant')
  await page.getByLabel('你的问题').fill('查询航班')
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect.poll(() => Boolean(stream)).toBe(true)
  await page.getByRole('button', { name: '停止', exact: true }).click()
  await expect(page.getByText('对话已停止', { exact: true })).toBeVisible()
  await expect(page.getByText('已完成', { exact: true })).toHaveCount(0)
})
