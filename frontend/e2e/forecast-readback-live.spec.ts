import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.skip(process.env.SCAT_FORECAST_READBACK !== '1', 'Explicit GET-only readback')
test('approved future forecast restores exact scope and trajectory without re-execution', async ({
  page,
}, info) => {
  const folder = resolve(import.meta.dirname, '../../var/validation/20261107-forecast-contract')
  const record = JSON.parse(readFileSync(resolve(folder, 'natural-acceptance.json'), 'utf8'))
  let writes = 0
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.name))
  await page.route('**/api/v1/**', (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      writes++
      return route.abort()
    }
    return route.continue()
  })
  if (record.status !== 'passed') {
    expect(record.planning_status).toBe('completed')
    expect(record.confirmations).toBe(1)
    const base = '/api/v1/hermes/threads/' + record.thread_id + '/model-tasks/' + record.task_id
    const response = await page.request.get(base)
    expect(response.ok()).toBe(true)
    const task = await response.json()
    expect(task.skill_job.state).toBe('succeeded')
    expect(task.workflow.proposal.kind).toBe('forecast')
    expect(task.workflow.proposal.observation_last_n).toBe(200)
    expect(task.skill_job.backend_executions).toHaveLength(1)
    expect(task.skill_job.backend_executions[0].calls).toBe(1)
    expect(task.run.result_artifact_ids).toHaveLength(2)
    expect(task.input_refs[0].end_time.replace('+00:00', 'Z')).toBe(
      task.workflow.prediction_contract.windows[0].forecast_origin.replace('+00:00', 'Z'),
    )
    for (const artifact of task.run.result_artifact_ids) {
      const body = await (await page.request.get(base + '/artifacts/' + artifact)).json()
      for (const block of body.analysis_result.blocks) {
        expect(
          (block.columns ?? []).some((c: { key: string }) =>
            /^(mae|rmse|ade|fde|error)_/.test(c.key),
          ),
        ).toBe(false)
        if (block.type === 'trajectory_2d') {
          expect(
            block.points.filter((p: { segment: string }) => p.segment === 'observed'),
          ).toHaveLength(200)
          const predicted = block.points.filter(
            (p: { segment: string }) => p.segment === 'predicted',
          )
          expect(predicted).toHaveLength(4)
          for (const [index, horizon] of [10, 30, 60].entries())
            expect(predicted[index + 1].time_step_s - predicted[0].time_step_s).toBeCloseTo(
              horizon,
              6,
            )
        }
      }
    }
    record.final_state = task.skill_job.state
    record.backend_executions = task.skill_job.backend_executions.map(
      (e: { id: string; state: string; calls: number }) => ({
        id: e.id,
        state: e.state,
        calls: e.calls,
      }),
    )
    record.artifact_ids = task.run.result_artifact_ids
    record.exact_observed_and_future_axis_verified = true
    record.no_error_metrics = true
    record.original_task_publication_restored_without_reexecution = true
    record.initial_live_browser_status = 'failed_on_mongo_input_time_projection_and_legacy_index'
  }
  await page.goto('/?thread_id=' + record.thread_id)
  await expect(page.getByText('从末次观测向未来预测', { exact: false })).toBeVisible()
  await expect(page.getByText('预测起点之后没有已知真值，不计算误差指标。')).toBeVisible()
  const buttons = page.getByRole('button', { name: /^查看预测轨迹/ })
  await page.getByRole('button', { name: '查看预测摘要', exact: true }).click()
  const panel = page.getByRole('region', { name: '结构化分析结果' })
  await expect(panel.getByText('已冻结观测窗口与预测起点')).toBeVisible()
  expect(await panel.locator('th').allTextContents()).not.toEqual(
    expect.arrayContaining(['MAE', 'RMSE', 'ADE', 'FDE']),
  )
  await buttons.last().click()
  await expect(panel.locator('.cesium-container')).toHaveAttribute(
    'data-point-count',
    /^(203|204)$/,
    { timeout: 60000 },
  )
  record.graph_points = Number(
    await panel.locator('.cesium-container').getAttribute('data-point-count'),
  )
  await page.reload()
  await buttons.last().click()
  await expect(panel.locator('.cesium-container')).toHaveAttribute(
    'data-point-count',
    String(record.graph_points),
    { timeout: 60000 },
  )
  expect(writes).toBe(0)
  expect(errors).toEqual([])
  record.status = 'passed'
  record.page_errors = errors
  writeFileSync(resolve(folder, 'natural-acceptance.json'), JSON.stringify(record, null, 2))
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  writeFileSync(
    resolve(folder, 'readback-' + info.project.name + '.json'),
    JSON.stringify(
      {
        status: 'passed',
        writes,
        page_errors: errors,
        graph_points: record.graph_points,
        refresh_restores: true,
      },
      null,
      2,
    ),
  )
})
