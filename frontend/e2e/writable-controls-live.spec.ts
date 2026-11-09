import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
test.skip(process.env.SCAT_WRITABLE_LIVE !== '1', 'Explicit opt-in for live writes')
test('real pending revocation and running worker cancellation from page controls', async ({
  page,
}) => {
  const folder = resolve(import.meta.dirname, '../../var/validation/20261107-writable-live')
  const input = JSON.parse(readFileSync(resolve(folder, 'case.json'), 'utf8'))
  const progress: Record<string, unknown> = {
    status: 'running',
    preparation:
      'real REST API for control-only cases; full conversational preparation is covered separately',
    stage: 'prepare',
  }
  const save = () =>
    writeFileSync(
      resolve(folder, 'browser-controls-receipt.json'),
      JSON.stringify(progress, null, 2),
    )
  const mutations: string[] = []
  page.on('request', (r) => {
    if (r.method() === 'POST') mutations.push(new URL(r.url()).pathname)
  })
  const response = await page.request.post('/api/v1/hermes/threads', { data: {} })
  expect(response.ok()).toBe(true)
  const tid = (await response.json()).thread_id
  const base = '/api/v1/hermes/threads/' + tid
  progress.thread_id = tid
  save()
  async function prepare(title: string) {
    const proposal = {
      kind: 'comparison',
      batch: input.batch,
      external_ids: [input.external_id],
      max_flights: 1,
      models: input.models.map((model: string) => ({ model })),
      horizons_s: [10, 30],
      max_cutoffs_per_flight: 1,
      title,
    }
    const res = await page.request.post(base + '/workflow-tasks', {
      data: { request_id: randomUUID(), proposal },
    })
    expect(res.ok()).toBe(true)
    return res.json()
  }
  const pending = await prepare('页面撤销待确认任务验收')
  progress.revoked_task_id = pending.task_id
  save()
  await page.goto('/assistant?thread_id=' + tid)
  await page.getByRole('button', { name: '撤销未执行任务', exact: true }).click()
  const revoked = await (await page.request.get(base + '/model-tasks/' + pending.task_id)).json()
  expect(revoked.state).toBe('revoked')
  expect(revoked.skill_job?.backend_executions ?? []).toHaveLength(0)
  progress.pending_revoked_without_execution = true
  progress.stage = 'prepare_cancellation'
  save()
  const running = await prepare('页面取消独立后台任务验收')
  progress.cancel_task_id = running.task_id
  save()
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await page.getByRole('button', { name: '确认参数并执行', exact: true }).click()
  const deadline = Date.now() + 90000
  while (
    Date.now() < deadline &&
    !(await page.getByRole('button', { name: '取消后台任务', exact: true }).count())
  ) {
    await page.waitForTimeout(1000)
    await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  }
  await expect(page.getByRole('button', { name: '取消后台任务', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '取消后台任务', exact: true }).click()
  progress.stage = 'cancellation_requested'
  save()
  let final = running
  const until = Date.now() + 180000
  while (Date.now() < until) {
    final = await (await page.request.get(base + '/model-tasks/' + running.task_id)).json()
    if (['cancelled', 'failed', 'succeeded', 'unknown', 'paused'].includes(final.skill_job?.state))
      break
    await page.waitForTimeout(2000)
  }
  progress.final_state = final.skill_job?.state
  progress.backend_executions = final.skill_job?.backend_executions
  save()
  expect(final.skill_job.state).toBe('cancelled')
  expect(
    (final.skill_job.backend_executions ?? []).every((x: { calls: number }) => x.calls <= 1),
  ).toBe(true)
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await expect(page.getByText('已取消', { exact: true })).toBeVisible()
  const before = mutations.length
  await page.reload()
  await expect(page.getByText('已取消', { exact: true })).toBeVisible()
  expect(mutations.length).toBe(before)
  expect(mutations.filter((p) => p.endsWith('/confirm'))).toHaveLength(1)
  expect(mutations.filter((p) => p.endsWith('/cancel'))).toHaveLength(1)
  await page.screenshot({
    path: resolve(import.meta.dirname, '../../docs/validation/20261107-writable-cancel.png'),
    fullPage: true,
    scale: 'css',
  })
  progress.status = 'passed'
  progress.stage = 'completed'
  progress.browser_mutations = mutations
  save()
})
