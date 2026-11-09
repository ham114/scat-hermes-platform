import { test, expect } from '@playwright/test'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
test.skip(
  process.env.SCAT_COMPLETION_LIVE !== '1' || process.env.SCAT_COMPLETION_SEND !== '1',
  'Explicit finite paid acceptance only',
)
const folder = resolve(
  import.meta.dirname,
  '../../var/validation/20261107-query-display-completion',
)
const questions = [
  { key: 'database', prompt: '目前能查询哪些数据库？各有多少数据？' },
  { key: 'trend', prompt: '查看SCAT里编号100002的航班，概括它的运动趋势，说明依据的数据范围。' },
  { key: 'plot', prompt: '把它的二维轨迹图画出来。', points: 524 },
  {
    key: 'models',
    prompt: '有哪些现在可用的轨迹预测模型？分别能配置哪些参数？先不要创建或运行预测任务。',
  },
  {
    key: 'fresh_plot',
    prompt: '帮我画出SCAT中编号100200航班的二维轨迹图，并简要说明飞行趋势。',
    points: 393,
  },
]
test('ordinary language queries and automatic complete plots after deployment', async ({
  page,
}) => {
  const file = resolve(folder, 'natural-acceptance.json')
  const start = Number(process.env.SCAT_COMPLETION_RESUME_AT ?? 0)
  if (existsSync(file) && start === 0)
    throw new Error(
      'Already admitted; use readback or explicit resume, never replay successful cases',
    )
  const report = existsSync(file)
    ? JSON.parse(readFileSync(file, 'utf8'))
    : { records: [], blocked_writes: 0, page_errors: [] }
  const save = () => writeFileSync(file, JSON.stringify(report, null, 2))
  page.on('pageerror', (e) => {
    report.page_errors.push(e.message)
    save()
  })
  await page.route('**/api/v1/**', (route) => {
    const r = route.request()
    if (
      !['GET', 'HEAD'].includes(r.method()) &&
      !/^\/api\/v1\/hermes\/threads(?:\/[a-f0-9-]+\/runs)?$/.test(new URL(r.url()).pathname)
    ) {
      report.blocked_writes++
      save()
      return route.abort()
    }
    return route.continue()
  })
  let tid = start ? report.records[0].thread_id : ''
  await page.goto(tid ? '/?thread_id=' + tid : '/')
  let consecutiveFailures = 0
  for (const [index, q] of questions.entries()) {
    if (index < start) continue
    if (report.records.some((r: { case: string }) => r.case === q.key))
      throw new Error('Case already admitted; do not resubmit')
    if (consecutiveFailures >= 2) {
      report.records.push({ case: q.key, status: 'skipped_after_two_failures' })
      save()
      continue
    }
    if (index === 4) await page.getByRole('button', { name: /新建会话/ }).click()
    await page.getByLabel('你的问题').fill(q.prompt)
    const admitted = page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        /\/hermes\/threads\/[^/]+\/runs$/.test(new URL(r.url()).pathname),
    )
    await page.getByRole('button', { name: '发送问题', exact: true }).click()
    const response = await admitted
    expect(response.ok()).toBe(true)
    const rid = (await response.json()).run_id
    tid = new URL(response.url()).pathname.split('/').at(-2)!
    const record: Record<string, unknown> = {
      case: q.key,
      prompt: q.prompt,
      thread_id: tid,
      run_id: rid,
      status: 'running',
    }
    report.records.push(record)
    save()
    const api = '/api/v1/hermes/threads/' + tid + '/runs/' + rid
    await expect
      .poll(async () => (await (await page.request.get(api)).json()).status, {
        timeout: 240000,
        intervals: [1500, 3000],
      })
      .toMatch(/^(completed|failed|cancelled|interrupted)$/)
    const state = await (await page.request.get(api)).json()
    record.status = state.status
    record.answer_characters = (state.output ?? '').length
    record.system_coverage = (state.output ?? '').startsWith('数据范围（系统核验）')
    record.guesses_plot_script = /plot_track\.py|script_not_published/.test(state.output ?? '')
    record.plot_refusal = /无法绘制|没有绘图工具/.test(state.output ?? '')
    record.models = ['constant-velocity-heading', 'kalman-cv'].filter((x) =>
      (state.output ?? '').includes(x),
    )
    consecutiveFailures = state.status === 'completed' ? 0 : consecutiveFailures + 1
    save()
    if (state.status !== 'completed') continue
    await page.goto('/?thread_id=' + tid)
    if (q.points) {
      const main = page.getByRole('region', { name: '本轮主结果', exact: true })
      await expect(main.locator('.cesium-container')).toHaveAttribute(
        'data-point-count',
        String(q.points),
        { timeout: 60000 },
      )
      await expect(page.getByText(new RegExp(`地图展示 ${q.points}/${q.points} 点`))).toBeVisible()
      record.automatic_plot = true
      record.display_points = q.points
      expect(await page.locator('details.source-details').getAttribute('open')).toBeNull()
      await page.reload()
      await expect(main.locator('.cesium-container')).toHaveAttribute(
        'data-point-count',
        String(q.points),
        { timeout: 60000 },
      )
      record.refresh_restores = true
    }
    if (q.key === 'models')
      expect(record.models).toEqual(['constant-velocity-heading', 'kalman-cv'])
    if (q.key === 'trend' || q.points) expect(record.system_coverage).toBe(true)
    expect(record.guesses_plot_script).toBe(false)
    expect(record.plot_refusal).toBe(false)
    save()
  }
  expect(report.blocked_writes).toBe(0)
  expect(report.records).toHaveLength(5)
  expect(report.records.every((r: { status: string }) => r.status === 'completed')).toBe(true)
  save()
})
