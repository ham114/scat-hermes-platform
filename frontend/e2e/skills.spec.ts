import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
test('skill author lifecycle requires explicit trial and separate release confirmation', async ({
  page,
}, info) => {
  const id = 'a'.repeat(32),
    tid = 'b'.repeat(32),
    sha = 'c'.repeat(64)
  const skill = {
    name: 'demo-skill',
    version: '1.0',
    requires_skills: { 'demo-child': '1.0' },
    allowed_operations: [],
    contract: { version: 1, inputs: ['batch'], outputs: ['report'] },
    files: { 'SKILL.md': '# Synthetic only' },
  }
  let draft = { draft: id, revision: 1, sha, base: sha, content: { changes: [skill] } }
  const calls: { method: string; path: string }[] = []
  await page.route('**/author-api/v1/skills/**', async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname.replace('/author-api/v1/skills', '')
    calls.push({ method: req.method(), path })
    let json: unknown = {}
    if (path === '/catalog') json = { bundle_sha256: sha, items: [skill] }
    else if (path === '/drafts' && req.method() === 'GET') json = { items: [] }
    else if (path === '/drafts' && req.method() === 'POST') {
      draft = { ...draft, content: req.postDataJSON().content }
      json = draft
    } else if (path === '/releases')
      json = { active: sha, history: [{ target: sha, previous: 'd'.repeat(64) }] }
    else if (path.endsWith('/diff'))
      json = { files: [{ path: 'demo-skill/SKILL.md', diff: '- old\n+ new' }] }
    else if (path.endsWith('/validate')) json = { valid: true }
    else if (path.endsWith('/trials') || path === '/trials/' + tid)
      json = {
        id: tid,
        draft: id,
        revision: 1,
        state: 'passed',
        report: { status: 'passed', synthetic_only: true },
      }
    else if (path === '/releases/preview')
      json = { id, sha256: sha, expires: Date.now() / 1000 + 300, details: req.postDataJSON() }
    else if (path === '/releases/confirm') {
      expect(req.headers()['x-scat-release-key']).toBe('synthetic-release')
      expect(req.postDataJSON().confirmed).toBe(true)
      json = { applied: true }
    }
    return route.fulfill({ json })
  })
  await page.goto('/skills')
  expect(calls).toEqual([])
  await page.getByLabel('作者凭据', { exact: true }).fill('synthetic-author')
  await page.getByRole('button', { name: '连接 / 刷新' }).click()
  await page.getByRole('button', { name: 'demo-skill · 1.0' }).click()
  await expect(page.getByText('demo-child', { exact: false }).first()).toBeVisible()
  await page.getByLabel('组合参数').fill('{"batch":"SYNTH"}')
  await page.getByRole('button', { name: '套用参数' }).click()
  await expect(page.getByText(/请使用 demo-skill/)).toContainText('SYNTH')
  expect(calls.every((x) => x.method === 'GET')).toBe(true)
  await page
    .getByLabel('草稿JSON')
    .fill(JSON.stringify({ changes: [{ ...skill, version: '1.1' }] }))
  await page.getByRole('button', { name: '创建草稿' }).click()
  await page.getByRole('button', { name: '差异比较' }).click()
  await expect(page.getByText(/old/)).toBeVisible()
  await expect(page.getByRole('button', { name: '开始试跑' })).toBeDisabled()
  await page.getByLabel('确认消耗额度执行当前修订试跑').check()
  await page.getByRole('button', { name: '开始试跑' }).click()
  await page.getByRole('button', { name: '发布预览', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认切换' })).toBeDisabled()
  expect(calls.some((x) => x.path === '/releases/confirm')).toBe(false)
  await page.getByLabel('已核对目标版本和完整差异').check()
  await page.getByLabel('独立发布凭据', { exact: true }).fill('synthetic-release')
  await page.getByRole('button', { name: '确认切换' }).click()
  await expect(page.getByRole('heading', { name: '切换确认' })).toHaveCount(0)
  expect(calls.filter((x) => x.path === '/releases/confirm')).toHaveLength(1)
  expect(
    await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage)),
  ).not.toContain('synthetic-author')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261106-skills-${info.project.name}.png`,
    ),
    fullPage: true,
    scale: 'css',
  })
  await page.getByRole('button', { name: '回滚预览' }).click()
  await expect(page.getByRole('heading', { name: '切换确认' })).toBeVisible()
  expect(calls.filter((x) => x.path === '/releases/confirm')).toHaveLength(1)
})
