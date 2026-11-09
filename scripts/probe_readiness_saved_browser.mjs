import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
import { writeFileSync } from 'node:fs'
import assert from 'node:assert/strict'

let credentials = JSON.parse(await new Promise(resolve => {
  let input = ''; process.stdin.setEncoding('utf8')
  process.stdin.on('data', s => input += s); process.stdin.on('end', () => resolve(input))
}))
const base = 'https://172.26.67.148:8444'
const thread = '42297b89-54c3-469d-94c0-13bccae5b9e2'
const task = '7b0dbcfd9cf846898ed3183b44a13e18'
const report = { status: 'running', kind: 'live_https_real_persisted_gbar', cases: [], network_mutations: 0, browser_bearers: 0 }
let browser
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true })
  for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    const context = await browser.newContext({ viewport, ignoreHTTPSErrors: true, httpCredentials: credentials })
    const page = await context.newPage(), errors = []
    page.on('pageerror', e => errors.push(e.name))
    page.on('request', r => { if (r.headers().authorization?.startsWith('Bearer ')) report.browser_bearers++ })
    await page.route('**/*', route => {
      if (!['GET', 'HEAD'].includes(route.request().method())) { report.network_mutations++; return route.abort() }
      return route.continue()
    })
    const source = await context.request.get(`${base}/api/v1/hermes/threads/${thread}/model-tasks/${task}/saved-model-output`)
    assert.equal(source.status(), 200)
    const artifact = await source.json()
    assert.equal(artifact.payload_sha256, 'b67d778f93ae1111a0694a312fddc233c209b3dfb0b0900f994553f34c3a4fe6')
    for (const refresh of [false, true]) {
      if (refresh) await page.reload()
      else await page.goto(`${base}/?thread_id=${thread}`)
      await page.getByRole('button', { name: '查看已保存模型输出', exact: true }).click({ timeout: 30000 })
      const analysis = page.getByRole('region', { name: '结构化分析结果', exact: true }).filter({ hasText: '已保存GBAR模型输出' }).last()
      await analysis.waitFor({ state: 'visible' })
      const types = [...new Set(artifact.analysis_result.blocks.map(b => b.type))]
      for (const type of types) assert.equal(await analysis.locator(`[data-block-type="${type}"]`).count() > 0, true)
      assert.equal(await analysis.locator('[data-block-type="prediction"]').count(), 6)
      const classification = analysis.locator('[data-block-type="classification"]')
      assert.equal(await classification.locator('tbody tr').count(), artifact.analysis_result.blocks.find(b => b.type === 'classification').items.length)
      await analysis.getByRole('button', { name: '打开轨迹交互', exact: true }).click()
      const workspace = analysis.getByRole('region', { name: '轨迹交互工作台', exact: true })
      await workspace.locator('.cesium-container[data-scene-dimension="2d"]').waitFor({ state: 'visible', timeout: 30000 })
      await workspace.getByRole('button', { name: '切换三维', exact: true }).click()
      await workspace.locator('.cesium-container[data-scene-dimension="3d"]').waitFor({ state: 'visible' })
      await workspace.getByRole('button', { name: '全屏', exact: true }).click()
      await page.waitForFunction(() => document.fullscreenElement !== null)
      await workspace.getByRole('button', { name: '退出全屏', exact: true }).click()
      const href = await analysis.getByRole('link', { name: /返回所属会话/ }).getAttribute('href')
      assert.equal(new URL(href, base).searchParams.get('thread_id'), thread)
      await analysis.getByLabel('展示调整', { exact: true }).fill('双列')
      await analysis.getByRole('button', { name: '调整展示', exact: true }).click()
      await analysis.getByRole('button', { name: '保存展示', exact: true }).click()
      assert.equal(await analysis.locator('.display-grid.dual').count(), 1)
      report.cases.push({ viewport: name, refresh, payload_sha256: artifact.payload_sha256, types, prediction_charts: 6, three_dimensional: true, fullscreen: true, saved_layout: true, owner_action_link: true })
    }
    assert.deepEqual(errors, [])
    await context.close()
  }
  assert.equal(report.network_mutations, 0)
  assert.equal(report.browser_bearers, 0)
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'; report.error_type = error.name; report.error = error.message.slice(0, 350); process.exitCode = 1
} finally {
  credentials = null
  await browser?.close()
  writeFileSync('var/validation/20261108-release-readiness/saved-gbar-browser.json', JSON.stringify(report, null, 2))
  process.stdout.write(JSON.stringify(report) + '\n')
}
