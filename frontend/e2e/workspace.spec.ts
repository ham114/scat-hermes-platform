import { expect, test } from '@playwright/test'
import { resolve } from 'node:path'
const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55'
const rid = 'run_history'
test('home stays quiet and dataset-free; browsing is explicit', async ({ page }, info) => {
  const calls: string[] = []
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.name))
  await page.route('**/api/v1/**', (route) => {
    calls.push(new URL(route.request().url()).pathname)
    return route.fulfill({ json: { items: [] } })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '有什么需要一起完成？' })).toBeVisible()
  await expect(page.getByText(/SCAT|UAV|MongoDB|数据集|批次/)).toHaveCount(0)
  expect(calls).toEqual([])
  await page.getByRole('button', { name: '检索文档', exact: true }).click()
  await expect(page.getByLabel('你的问题')).toHaveValue(/ADE/)
  expect(calls).toEqual([])
  await page.getByLabel('你的问题').fill('')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261028-workspace-home-${info.project.name}.png`,
    ),
    fullPage: true,
    scale: 'css',
  })
  if (info.project.name.includes('mobile'))
    await page.getByRole('button', { name: '打开导航' }).click()
  await page.getByRole('link', { name: '数据浏览', exact: true }).click()
  await expect(page.getByRole('heading', { name: '数据与结果' })).toBeVisible()
  await expect(page.getByRole('link', { name: '航班检索 航班与轨迹' })).toBeVisible()
  expect(calls).toEqual([])
  expect(errors).toEqual([])
})
test('history reads existing replies and continues the selected thread without a new admission', async ({
  page,
}, info) => {
  let posts = 0
  await page.route('**/api/v1/hermes/**', (route) => {
    if (route.request().method() !== 'GET') posts++
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/threads'))
      return route.fulfill({ json: { items: [{ thread_id: tid, created_at: 1793140000 }] } })
    if (path.endsWith('/messages'))
      return route.fulfill({
        json: {
          items: [
            {
              id: 1,
              request_id: tid,
              run_id: rid,
              input: '历史原始提问',
              input_available: true,
              output: '历史合成回复：完成两项检查。',
              status: 'completed',
              created_at: 1,
              updated_at: 2,
              last_sequence: 0,
            },
          ],
          next_cursor: null,
        },
      })
    if (path.endsWith('/runs'))
      return route.fulfill({ json: { items: [{ run_id: rid, request_id: tid }] } })
    if (path.endsWith('/runs/' + rid))
      return route.fulfill({
        json: {
          run_id: rid,
          status: 'completed',
          completed: true,
          output: '历史合成回复：完成两项检查。',
        },
      })
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/history')
  await page.getByRole('button', { name: /会话 ·/ }).click()
  await expect(page.getByText('历史合成回复：完成两项检查。', { exact: true })).toBeVisible()
  await expect(page.getByText('历史原始提问', { exact: true })).toBeVisible()
  expect(posts).toBe(0)
  await page.getByRole('link', { name: '继续对话' }).click()
  await expect(page.locator('.assistant-message.assistant')).toContainText('历史合成回复')
  await expect(page.getByLabel('你的问题')).toBeEnabled()
  expect(new URL(page.url()).searchParams.get('thread_id')).toBe(tid)
  expect(posts).toBe(0)
  await page.getByRole('button', { name: '新建会话' }).click()
  await expect(page.getByRole('heading', { name: '有什么需要一起完成？' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: '有什么需要一起完成？' })).toBeVisible()
  expect(posts).toBe(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  void info
})
test('sources are scoped reads on demand and tool calls stay visible individually', async ({
  page,
}, info) => {
  let writes = 0
  let sourceReads = 0
  const ref = {
    result_id: 'a'.repeat(32),
    payload_sha256: 'b'.repeat(64),
    created_at: 1793140000,
    expires_at: 9999999999,
    operation: 'search_scat_flights',
    kind: 'readonly_tool_result',
  }
  await page.route('**/api/v1/hermes/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'POST') writes++
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    if (path.endsWith('/threads')) return route.fulfill({ status: 201, json: { thread_id: tid } })
    if (path.endsWith('/runs'))
      return route.fulfill({ status: 202, json: { run_id: rid, status: 'started' } })
    if (path.endsWith('/events')) {
      const events = [
        { event: 'tool.started', tool: 'skill_script_run', tool_call_id: 'script' },
        { event: 'tool.completed', tool: 'skill_script_run', tool_call_id: 'script', error: false },
        { event: 'tool.started', tool: 'mcp__scat__search_scat_flights', tool_call_id: 'one' },
        {
          event: 'tool.completed',
          tool: 'mcp__scat__search_scat_flights',
          tool_call_id: 'one',
          error: false,
        },
        { event: 'tool.started', tool: 'mcp__scat__search_scat_flights', tool_call_id: 'two' },
        {
          event: 'tool.completed',
          tool: 'mcp__scat__search_scat_flights',
          tool_call_id: 'two',
          error: false,
        },
        {
          event: 'run.completed',
          completed: true,
          output: '合成验证：已完成检查，结果来源可查看。',
        },
      ]
      return route.fulfill({
        contentType: 'text/event-stream',
        body: events
          .map((e, i) => `id: ${i}\ndata: ${JSON.stringify({ ...e, run_id: rid })}\n\n`)
          .join(''),
      })
    }
    if (path.endsWith('/results')) {
      sourceReads++
      return route.fulfill({ json: { items: [ref] } })
    }
    if (path.endsWith('/results/' + ref.result_id)) {
      sourceReads++
      return route.fulfill({
        json: { ...ref, result: { status: 'ok', synthetic: true, data: { count: 2 } } },
      })
    }
    return route.fulfill({ json: { run_id: rid, status: 'running' } })
  })
  await page.goto('/')
  await page.getByLabel('你的问题').fill('合成验证')
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  expect(writes).toBe(2)
  expect(sourceReads).toBe(0)
  await page.getByText('执行过程 · 3 步', { exact: true }).click()
  await expect(page.getByText('航班检索 · 完成', { exact: true })).toHaveCount(2)
  await page.getByRole('button', { name: /查看来源/ }).click()
  await page.getByRole('button', { name: '查看数据', exact: true }).click()
  await expect(page.locator('.evidence-item pre')).toContainText('"count": 2')
  expect(writes).toBe(2)
  expect(sourceReads).toBe(2)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261028-workspace-chat-${info.project.name}.png`,
    ),
    fullPage: true,
    scale: 'css',
  })
})
test('Chinese composition does not send; Enter sends once', async ({ page }) => {
  let writes = 0
  await page.route('**/api/v1/hermes/**', (route) => {
    if (route.request().method() === 'POST') writes++
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/threads')) return route.fulfill({ status: 201, json: { thread_id: tid } })
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    return route.fulfill({
      json: { run_id: rid, status: 'completed', completed: true, output: '合成键盘验证' },
    })
  })
  await page.goto('/')
  await page.getByLabel('你的问题').fill('输入中文')
  await page
    .getByLabel('你的问题')
    .evaluate((el) =>
      el.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true }),
      ),
    )
  expect(writes).toBe(0)
  await page.getByLabel('你的问题').press('Shift+Enter')
  expect(writes).toBe(0)
  await page.getByLabel('你的问题').press('Enter')
  await expect(page.getByText('合成键盘验证', { exact: true })).toBeVisible()
  expect(writes).toBe(2)
})
test('failed history selection cannot send into the previous conversation', async ({ page }) => {
  let posts = 0
  await page.addInitScript(
    (id) =>
      sessionStorage.setItem(
        'scat-hermes-conversation-v1',
        JSON.stringify({ threadId: id, messages: [{ role: 'assistant', text: '旧会话' }] }),
      ),
    tid,
  )
  await page.route('**/api/v1/hermes/**', (route) => {
    if (route.request().method() === 'POST') posts++
    if (new URL(route.request().url()).pathname.endsWith('/model-tasks'))
      return route.fulfill({ json: { items: [] } })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/assistant?thread_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
  await expect(page.getByRole('alert')).toContainText('历史回复暂时不可用')
  await expect(page.getByLabel('你的问题')).toBeDisabled()
  await expect(page.getByRole('button', { name: '发送问题' })).toBeDisabled()
  expect(posts).toBe(0)
})
