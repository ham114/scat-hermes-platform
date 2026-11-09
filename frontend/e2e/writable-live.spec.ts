import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
test.skip(process.env.SCAT_WRITABLE_LIVE !== '1', 'Real writes require explicit maintainer opt-in')
const folder = resolve(import.meta.dirname, '../../var/validation/20261107-writable-live')
const receiptPath = resolve(folder, 'browser-live-receipt.json')
test('real conversation, page confirmation, independent CPU worker, result and refresh', async ({
  page,
}) => {
  const input = JSON.parse(readFileSync(resolve(folder, 'case.json'), 'utf8'))
  const mutations: { method: string; path: string }[] = []
  const browserAuth: string[] = []
  const receipt: Record<string, unknown> = {
    status: 'running',
    stage: 'opening',
    synthetic_backend: false,
    case: input,
  }
  const save = () => {
    mkdirSync(folder, { recursive: true })
    writeFileSync(receiptPath, JSON.stringify(receipt, null, 2))
  }
  save()
  page.on('request', (r) => {
    if (r.headers()['authorization']) browserAuth.push('unexpected')
    if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(r.method()))
      mutations.push({ method: r.method(), path: new URL(r.url()).pathname })
  })
  const catalog = await page.request.get('/api/v1/hermes/workflow-catalog')
  expect(catalog.status()).toBe(200)
  const capability = await catalog.json()
  expect(capability.enabled).toBe(true)
  expect(capability.synthetic).toBe(false)
  const proposal = {
    kind: 'comparison',
    batch: input.batch,
    external_ids: [input.external_id],
    max_flights: 1,
    models: input.models.map((model: string) => ({ model })),
    horizons_s: [10, 30],
    max_cutoffs_per_flight: 1,
    materializations: ['phase_segments'],
    missing_phase: 'build',
    title: '可写页面真实CPU对比验收',
  }
  const prompt =
    '这是用户授权的真实页面验收。请先加载 scat-model-comparison 技能与需要的依赖、查询工作流工具目录，然后仅准备一次以下真实SCAT比较方案：' +
    JSON.stringify(proposal) +
    '。只准备并等待我在页面确认；不要自行执行，不使用GPU，不训练，不修改原始集合。不得扩大样本范围。'
  receipt.question = prompt
  await page.goto('/assistant')
  await expect(page.getByLabel('你的问题')).toBeEnabled()
  await page.getByLabel('你的问题').fill(prompt)
  const threadResponse = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/hermes/threads',
  )
  const runResponse = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' && /\/threads\/[^/]+\/runs$/.test(new URL(r.url()).pathname),
    { timeout: 60000 },
  )
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  const thread = await (await threadResponse).json()
  receipt.thread_id = thread.thread_id
  const run = await runResponse
  expect(run.ok()).toBe(true)
  const initial = await run.json()
  receipt.parent_run_id = initial.run_id
  receipt.request_id = run.request().postDataJSON().request_id
  receipt.stage = 'planning'
  save()
  await expect(page.getByRole('button', { name: '确认参数并执行', exact: true })).toBeVisible({
    timeout: 260000,
  })
  const base = '/api/v1/hermes/threads/' + thread.thread_id
  const tasks = await (await page.request.get(base + '/model-tasks')).json()
  expect(tasks.items.length).toBe(1)
  const task = tasks.items[0]
  receipt.task_id = task.task_id
  receipt.execution_run_id = task.run_id
  receipt.stage = 'awaiting_confirmation'
  save()
  expect(task.state).toBe('pending')
  expect(task.workflow.synthetic).toBe(false)
  expect(task.workflow.coverage.selected_count).toBe(1)
  expect(task.workflow.proposal.models.map((x: { model: string }) => x.model)).toEqual(input.models)
  await page.screenshot({
    path: resolve(import.meta.dirname, '../../docs/validation/20261107-writable-confirm.png'),
    fullPage: true,
    scale: 'css',
  })
  expect(mutations.filter((x) => x.path.endsWith('/confirm'))).toHaveLength(0)
  await page.getByRole('button', { name: '确认参数并执行', exact: true }).click()
  receipt.stage = 'background_execution'
  save()
  let final = task
  const deadline = Date.now() + 300000
  while (Date.now() < deadline) {
    const res = await page.request.get(base + '/model-tasks/' + task.task_id)
    expect(res.ok()).toBe(true)
    final = await res.json()
    if (
      ['succeeded', 'failed', 'partial', 'cancelled', 'unknown', 'paused'].includes(
        final.skill_job?.state ?? final.run?.status ?? '',
      )
    )
      break
    await page.waitForTimeout(3000)
  }
  receipt.final_state = final.skill_job?.state
  receipt.backend_executions = final.skill_job?.backend_executions
  receipt.budget = final.skill_job?.budget
  receipt.stage = 'result'
  save()
  expect(final.skill_job?.state).toBe('succeeded')
  expect(final.run.status).toBe('succeeded')
  expect(final.skill_job.backend_executions.length).toBeGreaterThan(0)
  expect(final.skill_job.backend_executions.every((x: { calls: number }) => x.calls === 1)).toBe(
    true,
  )
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await expect(
    page.getByRole('button', { name: '查看分析结果', exact: true }).first(),
  ).toBeVisible()
  await page.getByRole('button', { name: '查看分析结果', exact: true }).first().click()
  const panel = page.getByRole('region', { name: '结构化分析结果' })
  await expect(panel).toBeVisible()
  expect(await panel.locator('[data-block-type]').count()).toBeGreaterThan(0)
  receipt.artifact_ids = final.run.result_artifact_ids
  receipt.rendered_block_types = await panel
    .locator('[data-block-type]')
    .evaluateAll((nodes) => nodes.map((n) => n.getAttribute('data-block-type')))
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      '../../docs/validation/20261107-writable-result-desktop.png',
    ),
    fullPage: true,
    scale: 'css',
  })
  await page.reload()
  await page.getByRole('button', { name: '查看分析结果', exact: true }).first().click()
  await expect(panel).toBeVisible()
  await page.setViewportSize({ width: 393, height: 851 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: resolve(import.meta.dirname, '../../docs/validation/20261107-writable-result-mobile.png'),
    fullPage: true,
    scale: 'css',
  })
  expect(mutations.filter((x) => /\/runs$/.test(x.path))).toHaveLength(1)
  expect(mutations.filter((x) => x.path.endsWith('/confirm'))).toHaveLength(1)
  expect(browserAuth).toEqual([])
  receipt.mutations = mutations
  receipt.browser_auth_headers = 0
  receipt.refresh_without_resubmission = true
  receipt.status = 'passed'
  receipt.stage = 'completed'
  save()
})
