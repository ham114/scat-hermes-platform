/** Read-only verification of the actual local frontend and remote S5 API. */
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const require = createRequire(resolve(root, 'frontend/package.json'))
const { chromium, expect } = require('@playwright/test')
const browser = await chromium.launch()
const report = { status: 'running', browserAuthorizationExposed: false, pages: [] }
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
  page.on('request', request => {
    if (request.url().startsWith('http://127.0.0.1:5173/api/') && request.headers().authorization) report.browserAuthorizationExposed = true
  })
  await expect.poll(async () => {
    try { return (await page.request.get('http://127.0.0.1:5173/api/v1/hermes/workflow-catalog', { timeout: 5000 })).status() }
    catch { return 0 }
  }, { timeout: 60000, intervals: [500, 1000, 2000, 5000] }).toBe(200)
  const response = await page.goto('http://127.0.0.1:5173/assistant')
  expect(response.status()).toBe(200)
  const summary = page.getByText('技能与工具组 · 复杂流程已启用', { exact: true })
  await expect(summary).toBeVisible({ timeout: 30000 })
  await summary.click()
  for (const name of ['固定样本', '数据物化', '模型与实验', '评估诊断', '报告']) {
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
  }
  await page.getByRole('button', { name: '填入合成双模型比较示例' }).click()
  await expect(page.getByLabel('你的问题')).toHaveValue(/SYNTH-S5/)
  await page.screenshot({ path: resolve(root, 'docs/validation/20261023-S5-live-assistant.png'), fullPage: true })
  report.pages.push({ path: '/assistant', status: 200, actualToolGroups: 5, exampleFilledWithoutSubmitting: true })
  const catalog = await page.request.get('http://127.0.0.1:5173/api/v1/hermes/workflow-catalog')
  expect(catalog.status()).toBe(200)
  const data = await catalog.json()
  expect(data.enabled && data.synthetic && !data.production_gpu).toBe(true)
  const health = await page.request.get('http://127.0.0.1:18080/health/ready')
  expect(health.status()).toBe(200)
  report.remoteReadiness = await health.json()
  expect(report.remoteReadiness.status).toBe('ok')
  await page.goto('http://127.0.0.1:5173/')
  await expect(page.locator('h1').first()).toBeVisible()
  await page.screenshot({ path: resolve(root, 'docs/validation/20261023-S5-live-portal.png'), fullPage: true })
  report.pages.push({ path: '/', status: 200 })
  expect(report.browserAuthorizationExposed).toBe(false)
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'
  report.errorType = error.name
  throw error
} finally {
  await browser.close()
  await writeFile(resolve(root, 'docs/validation/20261023-S5-live-preview.json'), JSON.stringify(report, null, 2) + '\n')
  process.stdout.write(JSON.stringify(report) + '\n')
}
