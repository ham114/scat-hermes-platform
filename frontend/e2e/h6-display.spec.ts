import { expect, test } from '@playwright/test'
import { resolve } from 'node:path'
import fixture from '../src/test-fixtures/analysis.json' with { type: 'json' }

const thread = '3d305763-d908-45c4-9692-c994a3cb5372'
const taskId = 'a'.repeat(32)

test('seven result blocks are readable on desktop and mobile without submitting work', async ({
  page,
}, info) => {
  let reads = 0
  let writes = 0
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.name))
  await page.addInitScript(
    ({ thread }) =>
      sessionStorage.setItem(
        'scat-hermes-conversation-v1',
        JSON.stringify({ threadId: thread, messages: [] }),
      ),
    { thread },
  )
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    if (request.method() !== 'GET') writes++
    const path = new URL(request.url()).pathname
    if (path.endsWith('/workflow-catalog'))
      return route.fulfill({
        json: {
          enabled: true,
          synthetic: true,
          groups: {},
          control_group: {},
          model_ids: [],
          production_gpu: false,
        },
      })
    if (path.endsWith('/model-tasks'))
      return route.fulfill({
        json: {
          items: [
            {
              task_id: taskId,
              run_id: fixture.run_id,
              state: 'finished',
              snapshot_sha256: 'd'.repeat(64),
              expires_at: 9999999999,
              parameters: {},
              input_refs: [],
              evaluator: null,
              components: {},
              code_version: 'h6-synthetic',
              model: {
                display_name: 'H6合成模型',
                model_key: 'synthetic',
                version: '1',
                limits: {},
              },
              run: { status: 'succeeded', progress: 1, result_artifact_ids: [fixture.artifact_id] },
            },
          ],
        },
      })
    if (path.endsWith('/artifacts/' + fixture.artifact_id)) {
      reads++
      expect(path).toContain(`/threads/${thread}/model-tasks/${taskId}/`)
      return route.fulfill({ json: fixture })
    }
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/assistant')
  await page.getByRole('button', { name: '查看分析结果' }).click()
  const panel = page.getByRole('region', { name: '结构化分析结果' })
  await expect(panel).toBeVisible()
  await expect(panel.locator('[data-block-type]')).toHaveCount(7)
  await expect(panel.locator('canvas')).toHaveCount(4)
  await expect(panel.getByText('12.75（m）', { exact: true })).toBeVisible()
  await expect(panel.getByText('未提供（m）', { exact: true })).toBeVisible()
  await page.getByLabel('预测快照', { exact: true }).selectOption('时刻B')
  await expect(page.getByText('轨迹数值明细 · 1 点', { exact: false })).toBeVisible()
  await panel.getByLabel('展示调整', { exact: true }).fill('双列；置顶 2；柱状图')
  await panel.getByRole('button', { name: '调整展示', exact: true }).click()
  await expect(panel.locator('.display-grid')).toHaveClass(/dual/)
  await expect(panel.locator('[data-block-type]').first()).toHaveAttribute(
    'data-block-type',
    'table',
  )
  await panel.getByRole('button', { name: '保存展示', exact: true }).click()
  await page.reload()
  await page.getByRole('button', { name: '查看分析结果' }).click()
  await expect(panel.locator('.display-grid')).toHaveClass(/dual/)
  await expect(panel.locator('[data-block-type]').first()).toHaveAttribute(
    'data-block-type',
    'table',
  )
  await panel.getByLabel('展示调整', { exact: true }).fill('重新训练')
  await panel.getByRole('button', { name: '调整展示', exact: true }).click()
  await expect(panel.getByRole('status')).toContainText('支持')
  expect(reads).toBe(2)
  expect(writes).toBe(0)
  expect(errors).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.getByLabel('预测快照', { exact: true }).selectOption('时刻A')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/${process.env.SCAT_E2E_SCREENSHOT_PREFIX ?? '20261106-H6A'}-h6-${info.project.name}.png`,
    ),
    fullPage: true,
    scale: 'css',
  })
  await page.reload()
  await page.getByRole('button', { name: '查看分析结果' }).click()
  await expect(page.getByRole('region', { name: '结构化分析结果' })).toBeVisible()
  expect(reads).toBe(3)
  expect(writes).toBe(0)
})

test('expired artifact is reported without retrying or creating a model task', async ({ page }) => {
  let artifactReads = 0
  await page.addInitScript(
    ({ thread }) =>
      sessionStorage.setItem(
        'scat-hermes-conversation-v1',
        JSON.stringify({ threadId: thread, messages: [] }),
      ),
    { thread },
  )
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    expect(route.request().method()).toBe('GET')
    if (path.endsWith('/model-tasks'))
      return route.fulfill({
        json: {
          items: [
            {
              task_id: taskId,
              run_id: fixture.run_id,
              state: 'finished',
              parameters: {},
              input_refs: [],
              components: {},
              evaluator: null,
              model: { display_name: '合成结果', limits: {} },
              run: { status: 'succeeded', progress: 1, result_artifact_ids: [fixture.artifact_id] },
            },
          ],
        },
      })
    if (path.includes('/artifacts/')) artifactReads++
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/assistant')
  await page.getByRole('button', { name: '查看分析结果' }).click()
  await expect(page.getByRole('alert').filter({ hasText: '结果暂时不可用' })).toBeVisible()
  expect(artifactReads).toBe(1)
  await expect(page.getByRole('region', { name: '结构化分析结果' })).toHaveCount(0)
})
