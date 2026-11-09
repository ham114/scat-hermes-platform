import { test, expect } from '@playwright/test'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.skip(process.env.SCAT_CLOSEOUT_NATURAL !== '1', 'One explicitly budgeted production GPU case')

test('natural submission, reviewed confirmation, formal native GPU worker, evaluation and live results', async ({
  page,
}) => {
  const file = resolve(
    import.meta.dirname,
    '../../var/validation/20261108-platform-closeout/natural-acceptance.json',
  )
  const previous = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) : undefined
  if (
    previous &&
    (previous.status !== 'admitted' ||
      previous.submissions ||
      previous.confirmations ||
      previous.planning_run_id)
  )
    throw new Error('Case already submitted; inspect it without replaying submission')
  const record: Record<string, unknown> = {
    status: 'admitted',
    new_questions: 1,
    confirmations: 0,
    max_gpu_posts: 1,
    pre_submission_harness_retry: previous ? 1 : 0,
  }
  const save = () => writeFileSync(file, JSON.stringify(record, null, 2))
  save()
  let submissions = 0,
    confirmations = 0,
    auth = 0
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.name))
  page.on('request', (r) => {
    if (r.headers().authorization) auth++
  })
  await page.route('**/api/v1/**', (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname
    if (!['GET', 'HEAD'].includes(request.method())) {
      if (/\/hermes\/threads$/.test(path)) return route.continue()
      if (/\/hermes\/threads\/[^/]+\/runs$/.test(path)) {
        if (++submissions > 1) return route.abort()
        record.submissions = submissions
        save()
        return route.continue()
      }
      if (/\/model-tasks\/[a-f0-9]{32}\/confirm$/.test(path)) {
        if (++confirmations > 1) return route.abort()
        record.confirmations = confirmations
        save()
        return route.continue()
      }
      return route.abort()
    }
    return route.continue()
  })
  await page.goto('/')
  const newChat = page.getByRole('button', { name: '新建会话', exact: true })
  if (await newChat.count()) await newChat.click()
  await page
    .getByLabel('你的问题')
    .fill(
      '请对批次scat20161015_20161021中的航班100002，取最后200个观测点，用GBAR从最后一个观测点向未来预测10、30、60秒。只处理这一架航班和一个预测起点，请准备方案等待我在页面确认；没有未来真值就不计算误差。',
    )
  const admitted = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' &&
      /\/hermes\/threads\/[^/]+\/runs$/.test(new URL(r.url()).pathname),
  )
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  const response = await admitted
  expect(response.ok()).toBe(true)
  const thread = new URL(response.url()).pathname.split('/').at(-2)!
  const run = (await response.json()).run_id
  record.thread_id = thread
  record.planning_run_id = run
  record.status = 'planning'
  save()
  const base = '/api/v1/hermes/threads/' + thread
  await expect
    .poll(async () => (await (await page.request.get(base + '/runs/' + run)).json()).status, {
      timeout: 240000,
      intervals: [2000, 3000],
    })
    .toMatch(/^(completed|failed|cancelled|interrupted)$/)
  const state = await (await page.request.get(base + '/runs/' + run)).json()
  record.planning_status = state.status
  save()
  expect(state.status).toBe('completed')
  const list = await (await page.request.get(base + '/model-tasks')).json()
  expect(list.items).toHaveLength(1)
  const task = list.items[0],
    p = task.workflow.proposal
  record.task_id = task.task_id
  record.execution_run_id = task.run_id
  record.status = 'awaiting_confirmation'
  save()
  expect(task.state).toBe('pending')
  expect(p.kind).toBe('forecast')
  expect(p.external_ids).toEqual(['100002'])
  expect(p.models.map((m: { model: string; version: string }) => [m.model, m.version])).toEqual([
    ['scat-gbar-v23', '23'],
  ])
  expect(p.observation_last_n).toBe(200)
  expect(p.forecast_origin).toBe('last_observation')
  expect(p.horizons_s).toEqual([10, 30, 60])
  expect(p.repeats).toBe(1)
  expect(task.source.run_id).toBe(run)
  expect(task.workflow.gpu.device).toBe('cuda')
  expect(task.workflow.prediction_contract.windows).toHaveLength(1)
  expect(task.workflow.prediction_contract.windows[0].point_count).toBe(200)
  expect(task.skill_job.state).toBe('awaiting_confirmation')
  await page.goto('/?thread_id=' + thread)
  const card = page.locator('.task-card').filter({ hasText: task.task_id })
  await expect(card.getByText('scat-gbar-v23:23', { exact: false })).toBeVisible()
  await expect(card.getByText('预测起点之后没有已知真值，不计算误差指标。')).toBeVisible()
  expect(confirmations).toBe(0)
  const confirmed = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' &&
      new URL(r.url()).pathname.endsWith(`/model-tasks/${task.task_id}/confirm`),
  )
  await card.getByRole('button', { name: '确认参数并执行', exact: true }).click()
  const queueResponse = await confirmed
  expect(queueResponse.status()).toBe(202)
  const queued = await queueResponse.json()
  record.queue_state = queued.skill_job.state
  record.approved_snapshot_sha256 = task.snapshot_sha256
  record.status = 'executing'
  save()
  expect(['queued', 'running']).toContain(queued.skill_job.state)
  const endpoint = base + '/model-tasks/' + task.task_id
  await expect
    .poll(async () => (await (await page.request.get(endpoint)).json()).skill_job.state, {
      timeout: 360000,
      intervals: [3000, 5000],
    })
    .toMatch(/^(succeeded|failed|partial|unknown|paused|cancelled)$/)
  const final = await (await page.request.get(endpoint)).json()
  record.final_state = final.skill_job.state
  record.budget = final.skill_job.budget
  record.backend_executions = final.skill_job.backend_executions
  record.artifact_ids = final.run.result_artifact_ids
  save()
  expect(final.skill_job.state).toBe('succeeded')
  expect(final.skill_job.backend_executions).toHaveLength(1)
  expect(final.skill_job.backend_executions[0].calls).toBe(1)
  expect(final.skill_job.budget.llm_calls).toBeLessThanOrEqual(24)
  expect(final.skill_job.budget.charged_tokens).toBeLessThanOrEqual(1000000)
  expect(final.skill_job.budget.exhausted).toBeNull()
  const successful = final.skill_job.steps
    .filter((s: { state: string }) => s.state === 'succeeded')
    .map((s: { operation: string }) => s.operation)
  expect(successful).toEqual(
    expect.arrayContaining([
      'scat_sample_prepare',
      'scat_trial_run',
      'scat_evaluation_summarize',
      'scat_evaluation_diagnose',
      'scat_report_build',
    ]),
  )
  expect(final.run.result_artifact_ids).toHaveLength(2)
  const hashes: string[] = []
  let graphPoints = 0
  for (const id of final.run.result_artifact_ids) {
    const artifact = await (await page.request.get(endpoint + '/artifacts/' + id)).json()
    hashes.push(artifact.payload_sha256)
    expect(artifact.analysis_result.context.prediction_mode).toBe('future_forecast')
    for (const b of artifact.analysis_result.blocks) {
      expect(
        (b.columns ?? []).some((c: { key: string }) => /^(mae|rmse|ade|fde|error)_/.test(c.key)),
      ).toBe(false)
      if (b.type === 'trajectory_2d') {
        const observed = b.points.filter((p: { segment: string }) => p.segment === 'observed')
        const predicted = b.points.filter((p: { segment: string }) => p.segment === 'predicted')
        expect(observed).toHaveLength(36)
        expect(predicted).toHaveLength(4)
        expect(predicted.map((p: { time_step_s: number }) => p.time_step_s)).toEqual([
          0, 10, 30, 60,
        ])
        graphPoints = b.points.length
      }
    }
  }
  record.artifact_hashes = hashes
  await page.reload()
  await card.getByRole('button', { name: '查看预测摘要', exact: true }).click()
  const panel = card.getByRole('region', { name: '结构化分析结果' })
  await expect(panel).toBeVisible()
  await card.getByRole('button', { name: /^查看预测轨迹/ }).click()
  const scene = panel.locator('.cesium-container').first()
  await expect(scene).toHaveAttribute('data-point-count', String(graphPoints), { timeout: 60000 })
  await expect(scene).toHaveAttribute('data-scene-dimension', '2d')
  await page.reload()
  await card.getByRole('button', { name: /^查看预测轨迹/ }).click()
  await expect(scene).toHaveAttribute('data-point-count', String(graphPoints), { timeout: 60000 })
  expect(submissions).toBe(1)
  expect(confirmations).toBe(1)
  expect(auth).toBe(0)
  expect(errors).toEqual([])
  record.status = 'passed'
  record.graph_points = graphPoints
  record.auth_headers = auth
  record.page_errors = errors
  record.refresh_without_resubmit = true
  record.maintenance_recovery_used = false
  save()
})
