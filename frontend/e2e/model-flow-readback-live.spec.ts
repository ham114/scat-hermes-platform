import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.skip(process.env.SCAT_MODEL_FLOW_READBACK !== '1', 'Explicit GET-only GBAR display checks')

const records = [
  {
    kind: 'forecast',
    thread: '21832620-2c51-4f86-9745-ecb1aedc63a4',
    task: '8121b9d0764e4ff584d4146fe2b57a39',
  },
  {
    kind: 'backtest',
    thread: 'e4e1a465-c389-4aff-b229-07dbb0268124',
    task: 'a1f321d6e9bd41699dbfe5f71fa34af0',
  },
]
if (process.env.SCAT_MODEL_FLOW_NEW_READBACK === '1') {
  const current = JSON.parse(
    readFileSync(
      resolve(
        import.meta.dirname,
        '../../var/validation/20261108-model-flow/direct-acceptance.json',
      ),
      'utf8',
    ),
  )
  if (current.status !== 'passed')
    throw new Error('New case must pass before GET-only display recovery')
  records.splice(0, records.length, {
    kind: 'new-forecast',
    thread: current.thread_id,
    task: current.task_id,
  })
}
for (const record of records) {
  test(`saved GBAR ${record.kind} displays real summary and trajectory without writes`, async ({
    page,
  }, info) => {
    let writes = 0,
      auth = 0
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.name))
    page.on('request', (r) => {
      if (r.headers().authorization) auth++
    })
    await page.route('**/api/v1/**', (route) => {
      if (!['GET', 'HEAD'].includes(route.request().method())) {
        writes++
        return route.abort()
      }
      return route.continue()
    })
    const base = `/api/v1/hermes/threads/${record.thread}/model-tasks/${record.task}`
    const response = await page.request.get(base)
    expect(response.ok()).toBe(true)
    const task = await response.json()
    expect(task.skill_job.state).toBe('succeeded')
    expect(task.skill_job.backend_executions[0].calls).toBe(1)
    expect(task.run.result_artifact_ids).toHaveLength(2)
    const artifact = await (
      await page.request.get(base + '/artifacts/' + task.run.result_artifact_ids[1])
    ).json()
    const block = artifact.analysis_result.blocks.find(
      (b: { type: string }) => b.type === 'trajectory_2d',
    )
    expect(block?.points.length).toBeGreaterThan(3)
    await page.goto('/?thread_id=' + record.thread)
    const card = page.locator('.task-card').filter({ hasText: record.task })
    const buttons = card.locator('.task-buttons button').filter({ hasText: /^查看/ })
    await buttons.first().click()
    const panel = card.getByRole('region', { name: '结构化分析结果' })
    await expect(panel).toBeVisible()
    await buttons.last().click()
    const scene = panel.locator('.cesium-container').first()
    await expect(scene).toHaveAttribute('data-point-count', String(block.points.length), {
      timeout: 60000,
    })
    await expect(scene).toHaveAttribute('data-scene-dimension', '2d')
    expect(await scene.getAttribute('data-camera-longitude')).not.toBeNull()
    await panel.getByRole('button', { name: '打开轨迹交互' }).click()
    const workspace = panel.getByRole('region', { name: '轨迹交互工作台', exact: true })
    await workspace.getByRole('button', { name: '切换三维' }).click()
    await expect(workspace.locator('.cesium-container')).toHaveAttribute(
      'data-scene-dimension',
      '3d',
    )
    await page.reload()
    await buttons.last().click()
    await expect(scene).toHaveAttribute('data-point-count', String(block.points.length), {
      timeout: 60000,
    })
    expect(errors).toEqual([])
    expect(writes).toBe(0)
    expect(auth).toBe(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    writeFileSync(
      resolve(
        import.meta.dirname,
        `../../var/validation/20261108-model-flow/old-${record.kind}-${info.project.name}.json`,
      ),
      JSON.stringify(
        {
          status: 'passed',
          writes,
          auth,
          page_errors: errors,
          displayed_points: block.points.length,
          refresh_restored: true,
          actual_gpu_calls: 0,
        },
        null,
        2,
      ),
    )
  })
}
