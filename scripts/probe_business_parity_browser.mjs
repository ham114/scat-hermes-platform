import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
import { readFileSync, writeFileSync, existsSync, copyFileSync } from 'node:fs'
import assert from 'node:assert/strict'

let { username, password } = JSON.parse(await new Promise((resolve) => {
  let input = ''; process.stdin.setEncoding('utf8');
  process.stdin.on('data', (s) => input += s); process.stdin.on('end', () => resolve(input))
}))
const output = 'var/validation/20261108-business-parity/'
if (existsSync(output + 'https-browser.json')) copyFileSync(output + 'https-browser.json', output + 'https-browser-attempt-' + Date.now() + '.json')
const record = JSON.parse(readFileSync(output + 'natural.json', 'utf8'))
const base = 'https://172.26.67.148:8444'
const report = { status: 'running', evidence: 'existing_live_https_results_no_model_replay', cases: [], writes: 0, browser_bearers: 0 }
const operations = { quality: 'inspect_scat_model_data_quality', phase_followup: 'get_scat_flight_phase_stats', uav_plot: 'get_uav_samples', uav_comparison: 'get_uav_normal_vs_anomaly_stats', empty: 'search_scat_flights' }
let browser, activePage
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true })
  for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    const context = await browser.newContext({ viewport, ignoreHTTPSErrors: true, httpCredentials: { username, password } })
    const page = await context.newPage(), errors = []
    activePage = page
    page.on('pageerror', (e) => errors.push(e.name))
    page.on('request', (r) => { if (r.headers().authorization?.startsWith('Bearer ')) report.browser_bearers++ })
    await page.route('**/*', (route) => {
      if (!['GET', 'HEAD'].includes(route.request().method())) { report.writes++; return route.abort() }
      return route.continue()
    })
    for (const row of record.cases) {
      assert.equal(row.status, 'passed')
      const ref = row.references.find((r) => r.operation === operations[row.id])
      const api = `/api/v1/hermes/threads/${row.thread_id}/runs/${row.run_id}/results/${ref.result_id}`
      const response = await context.request.get(base + api)
      assert.equal(response.status(), 200)
      const receipt = await response.json()
      assert.equal(receipt.payload_sha256, ref.payload_sha256)
      assert.equal(receipt.analysis_result.context.readonly_payload_sha256, ref.payload_sha256)
      let scenePoints = null
      for (const refresh of [false, true]) {
        const history = page.waitForResponse(r => new URL(r.url()).pathname === `/api/v1/hermes/threads/${row.thread_id}/messages`)
        if (refresh) await page.reload()
        else await page.goto(base + '/?thread_id=' + row.thread_id)
        assert.equal((await history).status(), 200)
        await page.locator('.history-placeholder').waitFor({state:'hidden'})
        const message = page.locator(`.assistant-message.assistant[data-run-id="${row.run_id}"]`).last()
        const toggle = message.getByRole('button', { name: /^查看来源/ })
        await toggle.waitFor({ state: 'visible' })
        if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
        const panel = message.getByRole('region', { name: '结果来源', exact: true })
        await panel.getByRole('button', { name: '查看数据', exact: true }).click()
        await panel.getByRole('region', { name: '本轮主结果', exact: true }).waitFor()
        const select = panel.getByLabel('展示范围', { exact: true })
        if (await select.count()) await select.selectOption(ref.result_id)
        const analysis = panel.getByRole('region', { name: '结构化分析结果', exact: true })
        await analysis.getByText(new RegExp(operations[row.id])).waitFor()
        assert.equal(await panel.getByRole('region', { name: '本轮主结果', exact: true }).count(), 1)
        if (row.id === 'quality') {
          await analysis.getByText('窗口检查（不执行预测）', { exact: true }).waitFor()
          await analysis.getByText('特征质量明细', { exact: true }).waitFor()
          assert.equal(await analysis.locator('tbody tr').count(), 6)
        }
        if (row.id === 'uav_plot') {
          await analysis.locator('.cesium-container[data-point-count="20"]').waitFor({ state: 'visible', timeout: 30000 })
          scenePoints = 20
          assert.equal(receipt.analysis_result.blocks.filter((b) => b.type === 'timeseries').length > 0, true)
          await analysis.getByRole('button', { name: '打开轨迹交互', exact: true }).click()
          const workspace = analysis.getByRole('region', { name: '轨迹交互工作台', exact: true })
          await workspace.getByRole('button', { name: '切换三维', exact: true }).click()
          await workspace.locator('.cesium-container[data-scene-dimension="3d"][data-point-count="20"]').waitFor({ state: 'visible' })
          if (!refresh) {
            const fullscreen = workspace.getByRole('button', { name: '全屏', exact: true })
            await fullscreen.click()
            await page.waitForFunction(() => document.fullscreenElement !== null)
            await workspace.getByRole('button', { name: '退出全屏', exact: true }).click()
          }
        }
      }
      report.cases.push({ viewport: name, id: row.id, payload_sha256: ref.payload_sha256, refresh: true, primary_results: 1, points: scenePoints })
    }
    if (name === 'desktop') {
      report.http_baseline = []
      for (const concurrency of [1, 2, 4]) {
        let next = 0; const samples = [], statuses = []
        await Promise.all(Array.from({ length: concurrency }, async () => {
          while (next++ < 12) {
            const start = performance.now()
            const response = await context.request.get(base + '/api/v1/hermes/workflow-catalog')
            await response.body()
            samples.push(performance.now() - start); statuses.push(response.status())
          }
        }))
        samples.sort((a,b) => a-b)
        assert.equal(statuses.every(s=>s===200), true)
        report.http_baseline.push({endpoint:'https_catalog_GET_only',requests:samples.length,concurrency,p50_ms:Math.round(samples[Math.ceil(samples.length*.5)-1]*100)/100,p95_ms:Math.round(samples[Math.ceil(samples.length*.95)-1]*100)/100,errors:0})
      }
    }
    const leaked = await page.evaluate((secret) => [...Object.values(localStorage), ...Object.values(sessionStorage)].some((s) => s.includes(secret)), password)
    assert.equal(leaked, false); assert.deepEqual(errors, [])
    await context.close()
  }
  assert.equal(report.writes, 0); assert.equal(report.browser_bearers, 0)
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'; report.error_type = error.name; report.error = error.message.slice(0, 300); process.exitCode = 1
  if (activePage) report.page_diagnostic = await activePage.locator('.assistant-message.assistant').evaluateAll(nodes => nodes.map(n => ({
    source_buttons: [...n.querySelectorAll('.message-footer button')].map(b => ({label:b.textContent,expanded:b.getAttribute('aria-expanded')})),
    statuses: [...n.querySelectorAll('.message-footer small')].map(s => s.textContent),
    source_panels: [...n.querySelectorAll('.run-evidence-body')].map(p => ({
      buttons: [...p.querySelectorAll('button')].map(b=>b.textContent),
      notices: [...p.querySelectorAll('[role="status"],[role="alert"]')].map(s=>s.textContent)
    }))
  })))
}
finally {
  username = ''; password = ''; await browser?.close()
  writeFileSync(output + 'https-browser.json', JSON.stringify(report, null, 2))
  process.stdout.write(JSON.stringify(report) + '\n')
}
