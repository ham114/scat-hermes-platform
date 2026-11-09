import { expect, test } from '@playwright/test'
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
test.skip(
  process.env.SCAT_QUERY_ROUNDS_LIVE !== '1' || process.env.SCAT_QUERY_ROUNDS_SEND !== '1',
  'Explicit finite real model test only',
)
const folder = resolve(import.meta.dirname, '../../var/validation/20261107-query-display-rounds')
const questions = [
  { key: 'database', prompt: '目前能查询哪些数据库？各有多少数据？' },
  { key: 'flight', prompt: '查看SCAT里编号100002的航班，概括它的运动趋势，说明依据的数据范围。' },
  { key: 'followup_plot', prompt: '把它的二维轨迹图画出来。' },
  {
    key: 'models',
    prompt: '有哪些现在可用的轨迹预测模型？分别能配置哪些参数？先不要创建或运行预测任务。',
  },
  { key: 'fresh_plot', prompt: '帮我画出SCAT中编号100200航班的二维轨迹图，并简要说明飞行趋势。' },
]
test('five bounded ordinary queries exercise skills, catalogs, plotting and refresh', async ({
  page,
}) => {
  mkdirSync(folder, { recursive: true })
  const resumeAt = Number(process.env.SCAT_QUERY_RESUME_AT ?? 0)
  const previous = resumeAt
    ? JSON.parse(readFileSync(resolve(folder, 'natural-rounds.json'), 'utf8'))
    : { records: [] }
  const records: Record<string, unknown>[] = previous.records
  let tid = '',
    rid = '',
    blockedWrites = 0,
    consecutiveFailures = 0
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('response', async (response) => {
    if (response.request().method() !== 'POST') return
    const path = new URL(response.url()).pathname
    const body = await response.json().catch(() => null)
    if (path.endsWith('/threads')) tid = body?.thread_id ?? tid
    if (path.endsWith('/runs')) rid = body?.run_id ?? rid
  })
  await page.route('**/api/v1/**', (route) => {
    const r = route.request()
    if (
      !['GET', 'HEAD'].includes(r.method()) &&
      !/^\/api\/v1\/hermes\/threads(?:\/[a-f0-9-]+\/runs)?$/.test(new URL(r.url()).pathname)
    ) {
      blockedWrites++
      return route.abort()
    }
    return route.continue()
  })
  const save = () =>
    writeFileSync(
      resolve(folder, 'natural-rounds.json'),
      JSON.stringify(
        {
          records,
          blocked_writes: blockedWrites,
          page_errors: errors,
          raw_trajectory_saved: false,
        },
        null,
        2,
      ),
    )
  if (resumeAt) tid = String(records[0]?.thread_id ?? '')
  await page.goto(tid ? '/?thread_id=' + tid : '/')
  for (const [index, q] of questions.entries()) {
    if (index < resumeAt) continue
    if (records.some((r) => r.case === q.key))
      throw new Error('Case already admitted; use GET-only readback, never resubmit')
    if (consecutiveFailures >= 2) {
      records.push({ case: q.key, status: 'skipped_after_two_failures' })
      save()
      continue
    }
    if (index === 4) {
      await page.getByRole('button', { name: '新建会话', exact: false }).click()
      tid = ''
      rid = ''
    }
    await page.getByLabel('你的问题').fill(q.prompt)
    const admission = page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        /\/hermes\/threads\/[^/]+\/runs$/.test(new URL(r.url()).pathname),
    )
    await page.getByRole('button', { name: '发送问题', exact: true }).click()
    const accepted = await admission
    expect(accepted.ok()).toBe(true)
    rid = (await accepted.json()).run_id
    tid = new URL(accepted.url()).pathname.split('/').at(-2)!
    const runPath = `/api/v1/hermes/threads/${tid}/runs/${rid}`
    records.push({ case: q.key, prompt: q.prompt, thread_id: tid, run_id: rid, status: 'running' })
    save()
    await expect
      .poll(async () => (await (await page.request.get(runPath)).json()).status, {
        timeout: 240000,
        intervals: [1500, 3000],
      })
      .toMatch(/^(completed|failed|cancelled|interrupted)$/)
    const history = await (
      await page.request.get(`/api/v1/hermes/threads/${tid}/messages?limit=20`)
    ).json()
    await page.goto('/?thread_id=' + tid)
    await expect(page.locator('.message-footer')).toHaveCount(history.items.length)
    const status = page.locator('.message-footer > small').last()
    await expect(status).toHaveText(/^(已完成|执行失败|对话已中断|对话已停止)$/, {
      timeout: 240000,
    })
    const state = await status.innerText()
    consecutiveFailures = state === '已完成' ? 0 : consecutiveFailures + 1
    const article = page
      .getByRole('article')
      .filter({ has: page.locator('.message-footer') })
      .last()
    const answer = await article
      .locator('.message-content > p')
      .innerText()
      .catch(() => '')
    const row: Record<string, unknown> = {
      case: q.key,
      prompt: q.prompt,
      thread_id: tid,
      run_id: rid,
      status: state,
      answer_characters: answer.length,
      mentions_missing_skill: /skill_required|skill_operation_not_allowed/.test(answer),
      mentions_plot_script: /plot_track\.py|script_not_published/.test(answer),
      mentions_no_plot_support: /无法.*绘|没有.*绘图/.test(answer),
      mentions_legacy_catalog: /scat_task_catalog/.test(answer),
      mentions_registered_models: ['constant-velocity-heading', 'kalman-cv'].filter((n) =>
        answer.includes(n),
      ),
    }
    records[records.findIndex((r) => r.run_id === rid)] = row
    save()
    if (state !== '已完成') continue
    await article.getByRole('button', { name: /查看来源/ }).click()
    const panel = article.getByRole('region', { name: '结果来源', exact: true })
    await expect(panel.getByText('正在读取…', { exact: true })).toHaveCount(0)
    if (await panel.getByRole('button', { name: '查看数据', exact: true }).count()) {
      await panel.getByRole('button', { name: '查看数据', exact: true }).click()
      await expect(panel.locator('.source-details')).toHaveCount(1, { timeout: 30000 })
      row.main_results = await panel.getByRole('region', { name: '本轮主结果' }).count()
      row.source_count = await panel.locator('.source-details article').count()
      const scene = panel.locator('.cesium-container')
      if (await scene.count()) {
        await expect(scene).toHaveAttribute('data-track-count', /^[1-9]/)
        row.displayed_points = Number(await scene.getAttribute('data-point-count'))
      }
    }
    if (q.key === 'followup_plot' || q.key === 'fresh_plot') {
      await page.reload()
      const reopened = page
        .getByRole('article')
        .filter({ has: page.locator('.message-footer') })
        .last()
      await reopened.getByRole('button', { name: /查看来源/ }).click()
      const panel = reopened.getByRole('region', { name: '结果来源', exact: true })
      await expect(panel.getByText('正在读取…', { exact: true })).toHaveCount(0)
      if (await panel.getByRole('button', { name: '查看数据', exact: true }).count()) {
        await panel.getByRole('button', { name: '查看数据', exact: true }).click()
        await expect(panel.locator('.cesium-container')).toHaveAttribute(
          'data-track-count',
          /^[1-9]/,
          { timeout: 30000 },
        )
        row.refresh_restores_plot = true
      }
    }
    save()
  }
  expect(blockedWrites).toBe(0)
  save()
})
