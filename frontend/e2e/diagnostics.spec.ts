import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'

const id = 'd'.repeat(32)
const sample = {
  trace: {
    id,
    run_id: 'run_test',
    owner: 'owner-test',
    thread_id: 'thread-test',
    request_id: 'request-test',
    profile: 'scat-main',
    state: 'completed',
    created: 1700000000,
    updated: 1700000010,
    label: '查询轨迹',
  },
  nodes: [
    {
      id: 'tool',
      parent_id: null,
      name: '查询轨迹',
      kind: 'tool',
      state: 'completed',
      started: 1700000000,
      ended: 1700000001,
      input: {},
      output: {},
      source: 'langfuse',
    },
    {
      id: 'receipt',
      parent_id: 'tool',
      name: '发布只读回执',
      kind: 'span',
      state: 'failed',
      started: 1700000001,
      ended: 1700000002,
      input: {},
      output: { status: 'receipt_unavailable' },
      source: 'platform',
    },
  ],
  related_tasks: [],
  langfuse_status: 'available',
  complete: true,
}
const payload = process.env.SCAT_DIAGNOSTICS_BROWSER_PAYLOAD
  ? (JSON.parse(
      readFileSync(process.env.SCAT_DIAGNOSTICS_BROWSER_PAYLOAD, 'utf8'),
    ) as typeof sample)
  : sample

test('diagnostic deep link shows the call tree and survives refresh without writes', async ({
  page,
}) => {
  let writes = 0
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/v1/**', async (route) => {
    if (route.request().method() !== 'GET') writes++
    if (new URL(route.request().url()).pathname.endsWith('/diagnostics/' + payload.trace.id))
      return route.fulfill({ json: payload })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/diagnostics/traces/' + payload.trace.id)
  const panel = page.getByRole('region', { name: '调用链排查' })
  await expect(panel.getByRole('heading', { name: '调用链详情' })).toBeVisible()
  await expect(panel.getByText('已读取 Langfuse 记录', { exact: false })).toBeVisible()
  await expect(panel.locator('.node')).toHaveCount(payload.nodes.length)
  await panel.getByLabel('仅看失败步骤').check()
  await expect(panel.locator('.node')).toHaveCount(
    payload.nodes.filter((node) => node.state === 'failed').length,
  )
  await page.reload()
  await expect(panel.locator('.node')).toHaveCount(payload.nodes.length)
  expect(writes).toBe(0)
  expect(errors).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})

test('unavailable Langfuse is visible while local evidence remains readable', async ({ page }) => {
  await page.route('**/api/v1/hermes/diagnostics/**', (route) =>
    route.fulfill({ json: { ...sample, langfuse_status: 'unavailable', complete: false } }),
  )
  await page.goto('/diagnostics/traces/' + id)
  await expect(page.getByRole('status')).toContainText('Langfuse 暂时不可用')
  await expect(
    page.getByRole('article', { name: '步骤详情' }).getByRole('heading', { name: '发布只读回执' }),
  ).toBeVisible()
})

test('long workflow step names wrap within the viewport', async ({ page }) => {
  const detail = {
    ...sample,
    nodes: sample.nodes.map((node) => ({
      ...node,
      name: 'prepare_scat_prediction_window_'.repeat(8),
    })),
  }
  await page.route('**/api/v1/hermes/diagnostics/**', (route) => route.fulfill({ json: detail }))
  await page.goto('/diagnostics/traces/' + id)
  await expect(page.getByRole('article', { name: '步骤详情' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
