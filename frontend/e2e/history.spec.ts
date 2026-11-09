import { expect, test } from '@playwright/test'
import { resolve } from 'node:path'
const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55'
const other = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const turn = (id: number) => ({
  id,
  request_id: tid,
  run_id: 'run_' + id,
  input: '原始问题 ' + id,
  input_available: true,
  output: '实际回复 ' + id,
  status: 'completed',
  created_at: id,
  updated_at: id,
  last_sequence: 1,
})
test('pages both lists and turns, renames with revision and resumes full chat', async ({
  page,
}, info) => {
  let renamed = 0,
    posts = 0
  await page.route('**/api/v1/hermes/**', (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      path = url.pathname
    if (req.method() === 'POST') posts++
    if (req.method() === 'PATCH') {
      renamed++
      expect(req.postDataJSON()).toEqual({ title: '新的会话名称', expected_revision: 1 })
      return route.fulfill({ json: { thread_id: tid, title: '新的会话名称', title_revision: 2 } })
    }
    if (path.endsWith('/threads'))
      return route.fulfill({
        json: {
          items: url.searchParams.has('cursor')
            ? [{ thread_id: other, created_at: 1, title: '更早会话', title_revision: 1 }]
            : [{ thread_id: tid, created_at: 2, title: '原来的会话名称', title_revision: 1 }],
          next_cursor: url.searchParams.has('cursor') ? null : 'next-threads',
        },
      })
    if (path.endsWith('/messages'))
      return route.fulfill({
        json: {
          items: [turn(url.searchParams.has('cursor') ? 1 : 2)],
          next_cursor: url.searchParams.has('cursor') ? null : 'older-turns',
        },
      })
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/history')
  await page.getByRole('button', { name: '更多会话' }).click()
  await expect(page.getByRole('button', { name: /更早会话/ })).toBeVisible()
  await page.getByRole('button', { name: /原来的会话名称/ }).click()
  await expect(page.getByText('原始问题 2', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '加载更早记录' }).click()
  await expect(page.locator('.history-reply')).toHaveCount(2)
  await expect(page.locator('.history-reply').first()).toContainText('原始问题 1')
  await page.getByRole('button', { name: '修改标题' }).click()
  await page.getByLabel('会话标题').fill('新的会话名称')
  await page.getByRole('button', { name: '保存标题' }).click()
  await expect(page.getByRole('heading', { name: '新的会话名称' })).toBeVisible()
  expect(renamed).toBe(1)
  expect(posts).toBe(0)
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261104-history-${info.project.name}.png`,
    ),
    fullPage: true,
    scale: 'css',
  })
  await page.getByRole('link', { name: '继续对话' }).click()
  await expect(page.locator('.assistant-message.user')).toContainText('原始问题 2')
  await page.getByRole('button', { name: '加载更早记录' }).click()
  await expect(page.locator('.assistant-message.user')).toHaveCount(2)
  await expect(page.getByLabel('你的问题')).toBeEnabled()
  expect(posts).toBe(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})
