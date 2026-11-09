import { test, expect } from '@playwright/test'
import { existsSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
test.skip(
  process.env.SCAT_MODEL_FLOW_DIRECT !== '1',
  'One explicit GPU task with reduced remaining budget',
)
test('direct browser parameters, server validation, page approval, native GPU/evaluation and saved charts', async ({
  page,
}) => {
  const file = resolve(
    import.meta.dirname,
    '../../var/validation/20261108-model-flow/direct-acceptance.json',
  )
  if (existsSync(file)) throw new Error('Already admitted; never replay this case')
  const record: Record<string, unknown> = {
    status: 'admitted',
    driver: 'direct_browser_form_and_native_worker',
    max_gpu_posts: 1,
    budget_units: 430041,
    planner_requests: 0,
  }
  const save = () => writeFileSync(file, JSON.stringify(record, null, 2))
  save()
  let preparations = 0,
    confirmations = 0,
    planners = 0,
    auth = 0
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.name))
  page.on('request', (r) => {
    if (r.headers().authorization) auth++
  })
  await page.route('**/api/v1/**', (route) => {
    const r = route.request(),
      path = new URL(r.url()).pathname
    if (!['GET', 'HEAD'].includes(r.method())) {
      if (path.endsWith('/workflow-tasks')) {
        if (++preparations > 1) return route.abort()
        record.preparations = preparations
        save()
        return route.continue()
      }
      if (/\/model-tasks\/[^/]+\/confirm$/.test(path)) {
        if (++confirmations > 1) return route.abort()
        record.confirmations = confirmations
        save()
        return route.continue()
      }
      if (/\/threads$/.test(path)) return route.continue()
      if (path.endsWith('/runs')) planners++
      return route.abort()
    }
    return route.continue()
  })
  await page.goto('/')
  await page.getByRole('button', { name: '流程模板', exact: true }).click()
  await page.getByText('直接准备预测方案', { exact: true }).click()
  const form = page.getByRole('form', { name: '直接预测参数' })
  await form.getByLabel('数据批次', { exact: true }).fill('scat20161015_20161021')
  await form.getByLabel('航班编号', { exact: true }).fill('100002')
  await form.getByLabel('预测模型', { exact: true }).selectOption('scat-gbar-v23')
  await form.getByLabel('最后观测点数', { exact: true }).fill('200')
  await form.getByLabel('未来时距（秒）', { exact: true }).fill('10,30,60')
  await form.getByText('后台运行额度', { exact: true }).click()
  await form.getByLabel('保守计量上限', { exact: true }).fill('430041')
  const prepared = page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/workflow-tasks'),
  )
  await form.getByRole('button', { name: '准备预测方案', exact: true }).click()
  const response = await prepared
  record.prepare_http = response.status()
  save()
  expect(response.status()).toBe(201)
  const task = await response.json(),
    proposal = task.workflow.proposal
  const thread = new URL(response.url()).pathname.split('/').at(-2)!
  record.thread_id = thread
  record.task_id = task.task_id
  record.execution_run_id = task.run_id
  record.approved_snapshot_sha256 = task.snapshot_sha256
  record.status = 'awaiting_confirmation'
  save()
  expect(task.state).toBe('pending')
  expect(proposal.kind).toBe('forecast')
  expect(proposal.observation_last_n).toBe(200)
  expect(proposal.horizons_s).toEqual([10, 30, 60])
  expect(proposal.models).toHaveLength(1)
  expect(proposal.models[0].model).toBe('scat-gbar-v23')
  expect(proposal.models[0].version).toBe('23')
  expect(task.workflow.prediction_contract.windows[0].point_count).toBe(200)
  expect(task.skill_job.budget.max_llm_tokens).toBe(430041)
  expect(task.skill_job.includes_parent_planning_usage).toBe(false)
  const card = page.locator('.task-card').filter({ hasText: task.task_id })
  await expect(card.getByText('scat-gbar-v23:23', { exact: false })).toBeVisible()
  const confirmed = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' &&
      new URL(r.url()).pathname.endsWith(`/model-tasks/${task.task_id}/confirm`),
  )
  await card.getByRole('button', { name: '确认参数并执行', exact: true }).click()
  const queue = await confirmed
  expect(queue.status()).toBe(202)
  record.queue_state = (await queue.json()).skill_job.state
  record.status = 'executing'
  save()
  expect(['queued', 'running']).toContain(record.queue_state)
  const base = `/api/v1/hermes/threads/${thread}/model-tasks/${task.task_id}`
  await expect
    .poll(async () => (await (await page.request.get(base)).json()).skill_job.state, {
      timeout: 360000,
      intervals: [3000, 5000],
    })
    .toMatch(/^(succeeded|partial|failed|unknown|paused|cancelled)$/)
  const final = await (await page.request.get(base)).json()
  record.final_state = final.skill_job.state
  record.backend_executions = final.skill_job.backend_executions
  record.budget = final.skill_job.budget
  record.artifact_ids = final.run.result_artifact_ids
  save()
  expect(final.skill_job.state).toBe('succeeded')
  expect(final.skill_job.backend_executions).toHaveLength(1)
  expect(final.skill_job.backend_executions[0].calls).toBe(1)
  expect(final.skill_job.budget.charged_tokens).toBeLessThanOrEqual(430041)
  expect(final.skill_job.budget.exhausted).toBeNull()
  expect(final.run.result_artifact_ids).toHaveLength(2)
  let graphPoints = 0
  const hashes: string[] = []
  for (const id of final.run.result_artifact_ids) {
    const body = await (await page.request.get(base + '/artifacts/' + id)).json()
    hashes.push(body.payload_sha256)
    expect(body.analysis_result.context.prediction_mode).toBe('future_forecast')
    for (const b of body.analysis_result.blocks) {
      expect(
        (b.columns ?? []).some((c: { key: string }) => /^(mae|rmse|ade|fde|error)_/.test(c.key)),
      ).toBe(false)
      if (b.type === 'trajectory_2d') {
        expect(b.points.filter((p: { segment: string }) => p.segment === 'observed')).toHaveLength(
          36,
        )
        expect(
          b.points
            .filter((p: { segment: string }) => p.segment === 'predicted')
            .map((p: { time_step_s: number }) => p.time_step_s),
        ).toEqual([0, 10, 30, 60])
        graphPoints = b.points.length
      }
    }
  }
  await page.goto('/?thread_id=' + thread)
  await card.getByRole('button', { name: '查看预测摘要', exact: true }).click()
  const panel = card.getByRole('region', { name: '结构化分析结果' })
  await expect(panel).toBeVisible()
  await card.getByRole('button', { name: /^查看预测轨迹/ }).click()
  const scene = panel.locator('.cesium-container').first()
  await expect(scene).toHaveAttribute('data-point-count', String(graphPoints), { timeout: 60000 })
  await page.reload()
  await card.getByRole('button', { name: /^查看预测轨迹/ }).click()
  await expect(scene).toHaveAttribute('data-point-count', String(graphPoints), { timeout: 60000 })
  expect(preparations).toBe(1)
  expect(confirmations).toBe(1)
  expect(planners).toBe(0)
  expect(auth).toBe(0)
  expect(errors).toEqual([])
  record.status = 'passed'
  record.artifact_hashes = hashes
  record.graph_points = graphPoints
  record.auth_headers = auth
  record.page_errors = errors
  record.refresh_without_resubmit = true
  record.maintenance_recovery_used = false
  save()
})
