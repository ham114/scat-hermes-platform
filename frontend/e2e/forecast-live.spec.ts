import { test, expect } from '@playwright/test'
import { existsSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.skip(
  process.env.SCAT_FORECAST_LIVE !== '1' || process.env.SCAT_FORECAST_SEND !== '1',
  'Explicit finite real acceptance only',
)
test('one natural request, one reviewed confirmation, one CPU forecast from exactly 200 points', async ({
  page,
}) => {
  const file = resolve(
    import.meta.dirname,
    '../../var/validation/20261107-forecast-contract/natural-acceptance.json',
  )
  if (existsSync(file)) throw new Error('Already admitted; inspect existing task, never replay')
  const record: Record<string, unknown> = { status: 'starting', new_questions: 1, confirmations: 0 }
  const save = () => writeFileSync(file, JSON.stringify(record, null, 2))
  let confirmations = 0
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.name))
  await page.route('**/api/v1/**', (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!['GET', 'HEAD'].includes(request.method())) {
      const allowed =
        /^\/api\/v1\/hermes\/threads(?:\/[a-f0-9-]+\/runs)?$/.test(path) ||
        /\/model-tasks\/[a-f0-9]{32}\/confirm$/.test(path)
      if (!allowed) return route.abort()
      if (path.endsWith('/confirm')) {
        if (++confirmations > 1) return route.abort()
        record.confirmations = confirmations
        save()
      }
    }
    return route.continue()
  })
  await page.goto('/')
  const prompt =
    '取SCAT中100002航班最后200个观测点，用当前可用的匀速模型，从最后一个观测点向未来预测10、30、60秒。请准备方案，等待页面确认；没有未来真值就不要计算误差。'
  await page.getByLabel('你的问题').fill(prompt)
  const admitted = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' &&
      /\/hermes\/threads\/[^/]+\/runs$/.test(new URL(r.url()).pathname),
  )
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  const response = await admitted
  expect(response.ok()).toBe(true)
  const tid = new URL(response.url()).pathname.split('/').at(-2)!
  const rid = (await response.json()).run_id
  record.thread_id = tid
  record.planning_run_id = rid
  record.status = 'planning'
  save()
  const base = '/api/v1/hermes/threads/' + tid
  await expect
    .poll(async () => (await (await page.request.get(base + '/runs/' + rid)).json()).status, {
      timeout: 240000,
      intervals: [2000, 3000],
    })
    .toMatch(/^(completed|failed|cancelled|interrupted)$/)
  const state = await (await page.request.get(base + '/runs/' + rid)).json()
  record.planning_status = state.status
  save()
  expect(state.status).toBe('completed')
  await page.goto('/?thread_id=' + tid)
  const tasks = await (await page.request.get(base + '/model-tasks')).json()
  expect(tasks.items).toHaveLength(1)
  const task = tasks.items[0]
  record.task_id = task.task_id
  record.execution_run_id = task.run_id
  record.status = 'awaiting_confirmation'
  save()
  expect(task.state).toBe('pending')
  expect(task.workflow.proposal.kind).toBe('forecast')
  expect(task.workflow.proposal.observation_last_n).toBe(200)
  expect(task.workflow.proposal.forecast_origin).toBe('last_observation')
  expect(task.workflow.proposal.external_ids).toEqual(['100002'])
  expect(task.workflow.proposal.models.map((m: { model: string }) => m.model)).toEqual([
    'constant-velocity-heading',
  ])
  expect(task.workflow.proposal.horizons_s).toEqual([10, 30, 60])
  expect(task.workflow.prediction_contract.windows).toHaveLength(1)
  expect(task.workflow.prediction_contract.windows[0].point_count).toBe(200)
  await expect(page.getByText('从末次观测向未来预测', { exact: false })).toBeVisible()
  await expect(page.getByText('预测起点之后没有已知真值，不计算误差指标。')).toBeVisible()
  await page.getByRole('button', { name: '确认参数并执行', exact: true }).click()
  record.status = 'executing'
  save()
  await expect
    .poll(
      async () => {
        const value = await (await page.request.get(base + '/model-tasks/' + task.task_id)).json()
        return value.skill_job?.state ?? value.run?.status
      },
      { timeout: 360000, intervals: [3000, 5000] },
    )
    .toMatch(/^(succeeded|failed|partial|unknown|paused|cancelled)$/)
  const final = await (await page.request.get(base + '/model-tasks/' + task.task_id)).json()
  record.final_state = final.skill_job.state
  record.backend_executions = final.skill_job.backend_executions.map(
    (e: { id: string; state: string; calls: number }) => ({
      id: e.id,
      state: e.state,
      calls: e.calls,
    }),
  )
  record.artifact_ids = final.run.result_artifact_ids
  save()
  expect(final.skill_job.state).toBe('succeeded')
  expect(final.skill_job.backend_executions).toHaveLength(1)
  expect(final.skill_job.backend_executions[0].calls).toBe(1)
  expect(final.run.result_artifact_ids).toHaveLength(2)
  for (const artifact of final.run.result_artifact_ids) {
    const body = await (
      await page.request.get(base + '/model-tasks/' + task.task_id + '/artifacts/' + artifact)
    ).json()
    expect(body.analysis_result.context.prediction_mode).toBe('future_forecast')
    if (body.analysis_result.operation === 'point_exact_prediction') {
      const path = body.analysis_result.blocks.find(
        (b: { type: string }) => b.type === 'trajectory_2d',
      )
      expect(path.points.filter((p: { segment: string }) => p.segment === 'observed')).toHaveLength(
        200,
      )
      const predicted = path.points.filter((p: { segment: string }) => p.segment === 'predicted')
      expect(predicted).toHaveLength(4)
      const origin = predicted[0].time_step_s
      expect(
        predicted.slice(1).map((p: { time_step_s: number }) => p.time_step_s - origin),
      ).toEqual([10, 30, 60])
      record.exact_observed_and_future_axis_verified = true
    }
    for (const block of body.analysis_result.blocks) {
      expect(
        (block.columns ?? []).some((c: { key: string }) =>
          /^(mae|rmse|ade|fde|error)_/.test(c.key),
        ),
      ).toBe(false)
    }
  }
  await page.reload()
  await page
    .getByRole('button', { name: /^查看预测轨迹/ })
    .last()
    .click()
  const graph = page.getByRole('region', { name: '结构化分析结果' })
  await expect(graph.locator('.cesium-container')).toHaveAttribute(
    'data-point-count',
    /^(203|204)$/,
    {
      timeout: 60000,
    },
  )
  record.graph_points = Number(
    await graph.locator('.cesium-container').getAttribute('data-point-count'),
  )
  record.no_error_metrics = true
  record.page_errors = errors
  record.status = 'passed'
  save()
  expect(errors).toEqual([])
})
