import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
import { writeFileSync, existsSync, copyFileSync } from 'node:fs'
import assert from 'node:assert/strict'
const report = { status: 'running', kind: 'real_old_streamlit_renderer_harness_not_full_legacy_business_parity', cases: [], model_calls: 0 }
const destination = 'var/validation/20261108-release-readiness/legacy-renderer-browser.json'
if (existsSync(destination)) copyFileSync(destination, destination.replace('.json', '-attempt-' + Date.now() + '.json'))
let browser, page
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true })
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  await page.goto('http://127.0.0.1:18136')
  await page.getByText('BLOCK_TYPES=', { exact: false }).waitFor({ timeout: 60000 })
  assert.equal(await page.getByTestId('stException').count(), 0)
  await page.getByText('SOURCE_SHA256=b67d778f93ae1111a0694a312fddc233c209b3dfb0b0900f994553f34c3a4fe6', { exact: false }).waitFor()
  report.cases.push({ source: 'real_saved_gbar', types: ['metrics', 'table', 'trajectory_2d', 'prediction', 'classification', 'action_link'], source_sha256: 'b67d778f93ae1111a0694a312fddc233c209b3dfb0b0900f994553f34c3a4fe6', dataframes: await page.getByTestId('stDataFrame').count(), metrics: await page.getByTestId('stMetric').count(), link_href: await page.getByRole('link', { name: '返回所属会话', exact: true }).getAttribute('href') })
  const map = page.getByTestId('stSelectbox').filter({ hasText: '背景地图' }).getByRole('combobox')
  await map.click()
  await page.getByRole('option', { name: '无底图', exact: true }).click()
  await page.getByText('BLOCK_TYPES=', { exact: false }).waitFor()
  await page.getByText('放大查看', { exact: true }).click()
  assert.equal(await page.getByRole('checkbox', { name: '放大查看', exact: true }).isChecked(), true)
  await page.reload()
  await page.getByText('BLOCK_TYPES=', { exact: false }).waitFor()
  await page.getByTestId('stSelectbox').filter({ hasText: '真实制品' }).getByRole('combobox').click()
  await page.getByRole('option', { name: 'UAV真实已保存样本', exact: true }).click()
  await page.getByText('BLOCK_TYPES=metrics,timeseries,trajectory_2d', { exact: false }).waitFor()
  assert.equal(await page.getByTestId('stException').count(), 0)
  const source = await page.locator('[data-testid="stCode"]').innerText()
  report.cases.push({ source: 'real_saved_uav', types: ['metrics', 'timeseries', 'trajectory_2d'], source_reference: source.trim(), reload: true })
  report.testids = await page.locator('[data-testid]').evaluateAll(nodes => [...new Set(nodes.map(n => n.dataset.testid))])
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'; report.error_type = error.name; report.error = error.message.slice(0, 500); process.exitCode = 1
  if (page) report.control_labels = await page.locator('[data-testid="stSelectbox"], [data-testid="stCheckbox"], [data-testid="stToggle"]').allTextContents()
} finally {
  await browser?.close()
  writeFileSync(destination, JSON.stringify(report, null, 2))
  process.stdout.write(JSON.stringify(report) + '\n')
}
