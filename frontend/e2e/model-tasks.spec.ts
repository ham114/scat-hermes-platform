import { expect, test } from '@playwright/test'
import { resolve } from 'node:path'

const tid = '9d8e81e1-4e45-445b-a05b-42292797991c'
const task = {
  task_id: 'a'.repeat(32),
  run_id: 'b'.repeat(32),
  state: 'pending',
  snapshot_sha256: 'c'.repeat(64),
  expires_at: 9999999999,
  parameters: { horizon: 36 },
  input_refs: [{ dataset: 'scat', external_id: 'flight-1', batch: 'test-batch', limit: 71 }],
  evaluator: { key: 'error-metrics', version: '1' },
  components: { model_package: { key: 'gbar-package', version: '23' } },
  code_version: 'H5-validation',
  model: {
    display_name: '受控模型参数确认示例',
    model_key: 'test-model',
    version: '1',
    limits: { max_batch_size: 1 },
  },
}

test('human confirmation and refresh preserve one durable run', async ({ page }, info) => {
  let confirms = 0
  let state = 'pending'
  await page.addInitScript((id) => {
    sessionStorage.setItem(
      'scat-hermes-conversation-v1',
      JSON.stringify({ threadId: id, messages: [] }),
    )
  }, tid)
  await page.route('**/api/v1/hermes/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/confirm')) {
      confirms++
      expect(route.request().postDataJSON()).toEqual({
        confirmed: true,
        snapshot_sha256: task.snapshot_sha256,
      })
      state = 'queued'
      return route.fulfill({ status: 202, json: { ...task, state } })
    }
    if (path.endsWith('/model-tasks'))
      return route.fulfill({ json: { items: [{ ...task, state }] } })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/assistant')
  await expect(page.getByText('flight-1', { exact: false })).toBeVisible()
  expect(confirms).toBe(0)
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/${process.env.SCAT_E2E_SCREENSHOT_PREFIX ?? '20261022-H5-confirmation'}-tasks-${info.project.name}.png`,
    ),
    fullPage: true,
  })
  await page.getByRole('button', { name: '确认参数并执行' }).click()
  await expect(page.getByText('已提交，等待 worker', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByText('已提交，等待 worker', { exact: true })).toBeVisible()
  expect(confirms).toBe(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})

test('unknown GPU outcome has no resubmit button', async ({ page }) => {
  let posts = 0
  await page.addInitScript((id) => {
    sessionStorage.setItem(
      'scat-hermes-conversation-v1',
      JSON.stringify({ threadId: id, messages: [] }),
    )
  }, tid)
  await page.route('**/api/v1/hermes/**', async (route) => {
    if (route.request().method() === 'POST') posts++
    return route.fulfill({ json: { items: [{ ...task, state: 'unknown' }] } })
  })
  await page.goto('/assistant')
  await expect(page.getByText('状态待核实，禁止自动重跑', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '确认参数并执行' })).toHaveCount(0)
  await page.reload()
  await expect(page.getByText('状态待核实，禁止自动重跑', { exact: true })).toBeVisible()
  expect(posts).toBe(0)
})
