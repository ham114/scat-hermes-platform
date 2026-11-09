import { expect, test } from '@playwright/test'
test('live preview is readonly and browser requests contain no authentication token', async ({
  page,
}) => {
  const authHeaders: string[] = []
  page.on('request', (request) => {
    const value = request.headers()['authorization']
    if (value) authHeaders.push(value)
  })
  await page.goto('/')
  await expect(page.getByText('只读预览', { exact: true })).toBeVisible()
  await expect(page.getByLabel('你的问题')).toBeDisabled()
  await expect(page.getByRole('button', { name: '发送问题' })).toBeDisabled()
  expect(authHeaders).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})
test('real backend metadata is readable and writes are blocked at a nonexistent probe path', async ({
  request,
}) => {
  const threads = await request.get('/api/v1/hermes/threads?limit=1')
  expect(threads.status()).toBe(200)
  const body = await threads.json()
  expect(Array.isArray(body.items)).toBe(true)
  expect(body.items.length).toBeLessThanOrEqual(1)
  const catalog = await request.get('/api/v1/hermes/workflow-catalog')
  expect(catalog.status()).toBe(200)
  expect(typeof (await catalog.json()).enabled).toBe('boolean')
  // This probe path does not exist on the backend, even if the guard regresses.
  const denied = await request.post('/api/v1/__readonly_probe__', { data: {} })
  expect(denied.status()).toBe(405)
})
