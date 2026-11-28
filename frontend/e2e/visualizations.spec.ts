import { expect, test } from '@playwright/test'
import { deflateSync } from 'node:zlib'
import { join } from 'node:path'

const thread = '3d305763-d908-45c4-9692-c994a3cb5372'
const first = 'a'.repeat(32),
  second = 'b'.repeat(32)
const artifact = 'c'.repeat(32),
  revised = 'd'.repeat(32)
const sha = 'e'.repeat(64)

function png(width: number, height: number) {
  const chunk = (type: string, data: Buffer) => {
    const body = Buffer.concat([Buffer.from(type), data])
    let crc = 0xffffffff
    for (const byte of body) {
      crc ^= byte
      for (let i = 0; i < 8; i++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0)
    }
    const size = Buffer.alloc(4),
      checksum = Buffer.alloc(4)
    size.writeUInt32BE(data.length)
    checksum.writeUInt32BE((crc ^ 0xffffffff) >>> 0)
    return Buffer.concat([size, body, checksum])
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width)
  header.writeUInt32BE(height, 4)
  header[8] = 8
  header[9] = 2
  const rows = []
  for (let y = 0; y < height; y++) {
    const row = Buffer.alloc(1 + width * 3, 255)
    row[0] = 0
    for (let x = 0; x < width; x++) {
      const bar =
        (x > width * 0.15 && x < width * 0.35 && y > height * 0.45 && y < height * 0.8) ||
        (x > width * 0.55 && x < width * 0.75 && y > height * 0.2 && y < height * 0.8)
      if (bar) {
        row[1 + x * 3] = 37
        row[2 + x * 3] = 99
        row[3 + x * 3] = 235
      }
    }
    rows.push(row)
  }
  return Buffer.concat([
    Buffer.from('89504e470d0a1a0a', 'hex'),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(Buffer.concat(rows))),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

test('delivers reviewed charts, revises with natural feedback, and restores history without POST', async ({
  page,
}, info) => {
  let posts = 0,
    revisionPolls = 0,
    feedback = ''
  let created = false,
    revision = false
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('console', (message) => {
    if (['error', 'warning'].includes(message.type())) errors.push(message.text())
  })
  await page.route('**/portal-session', (route) =>
    route.fulfill({ json: { username: 'qa', owner_id: 'visualization-qa', role: 'user' } }),
  )
  const task = (id: string, version = 1) => ({
    task_id: id,
    state: version === 2 && revisionPolls++ < 1 ? 'running' : 'succeeded',
    phase: 'reviewing',
    intent: version === 2 ? feedback : '比较当前数据',
    created_at: 1,
    artifact_id: version === 2 ? revised : artifact,
    parent_artifact_id: version === 2 ? artifact : null,
    version,
    memory_outcome: version === 1 ? 'unconfirmed' : 'not_requested',
  })
  await page.route('**/api/v1/hermes/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'POST') posts++
    if (path.endsWith('/threads'))
      return route.fulfill({ status: 201, json: { thread_id: thread } })
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    if (path.endsWith('/runs')) {
      created = true
      return route.fulfill({
        status: 202,
        json: {
          run_id: 'run_chart',
          status: 'completed',
          output: '自主图表已提交后台，审核后展示。',
          completed: true,
        },
      })
    }
    if (path.endsWith('/visualization-tasks'))
      return route.fulfill({
        json: {
          enabled: true,
          items: created ? [...(revision ? [task(second, 2)] : []), task(first)] : [],
        },
      })
    if (path.endsWith('/revisions')) {
      feedback = route.request().postDataJSON().feedback
      revision = true
      return route.fulfill({ status: 202, json: task(second, 2) })
    }
    if (path.includes('/files/'))
      return route.fulfill({
        contentType: 'image/png',
        body: path.endsWith('mobile.png') ? png(390, 640) : png(1200, 720),
        headers: { 'Cache-Control': 'no-store' },
      })
    if (path.includes('/visualizations/')) {
      const version = path.endsWith(revised) ? 2 : 1
      return route.fulfill({
        json: {
          artifact_id: version === 2 ? revised : artifact,
          task_id: version === 2 ? second : first,
          schema_version: 1,
          version,
          parent_artifact_id: version === 2 ? artifact : null,
          intent: version === 2 ? feedback : '比较当前数据',
          created_at: 1,
          snapshot_sha256: sha,
          source_sha256: sha,
          review_id: first,
          viewports: ['desktop', 'mobile'],
          data_refs: [
            { kind: 'readonly_result', source_id: first, run_id: 'run_chart', payload_sha256: sha },
          ],
          files: { 'desktop.png': sha, 'mobile.png': sha, 'source.js': sha },
        },
      })
    }
    return route.fulfill({ json: { items: [] } })
  })
  await page.goto('/assistant')
  await expect(page).toHaveURL(/\/assistant$/)
  await expect(page).toHaveTitle('对话 · Hermes')
  await expect(page.getByLabel('你的问题')).toBeVisible()
  await page.getByLabel('你的问题').fill('请用自主可视化比较当前数据')
  await page.getByRole('button', { name: '发送问题' }).click()
  await expect(page.getByRole('heading', { name: '自主图表 · 版本 1' })).toBeVisible()
  await page.getByLabel('图表修改要求').fill('将图例放在底部，增大字号，保留原数据。')
  await expect(
    page.getByText('未收到完整的偏好确认，未保存新的长期偏好；可在记忆页核对。'),
  ).toBeVisible()
  if (process.env.SCAT_VIS_QA_DIR)
    await page.screenshot({
      path: join(process.env.SCAT_VIS_QA_DIR, `delegation-unconfirmed-${info.project.name}.png`),
    })
  await page.getByRole('button', { name: '生成新版本' }).click()
  await expect(page.getByRole('heading', { name: '自主图表 · 版本 2' })).toBeVisible({
    timeout: 10000,
  })
  await expect(page.getByRole('heading', { name: '自主图表 · 版本 1' })).toBeVisible()
  expect(feedback).toContain('图例放在底部')
  const posted = posts
  await page.reload()
  await expect(page.getByRole('heading', { name: '自主图表 · 版本 2' })).toBeVisible()
  expect(posts).toBe(posted)
  await expect(page.getByRole('link', { name: '下载宽屏 PNG' }).first()).toHaveAttribute(
    'href',
    /files\/desktop\.png$/,
  )
  await expect(page.getByRole('link', { name: '下载绘图代码' }).first()).toHaveAttribute(
    'download',
    'source.js',
  )
  await expect
    .poll(() =>
      page
        .locator('picture img')
        .first()
        .evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0),
    )
    .toBe(true)
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(errors).toEqual([])
  if (process.env.SCAT_VIS_QA_DIR) {
    await page.getByRole('heading', { name: '自主图表 · 版本 2' }).scrollIntoViewIfNeeded()
    await page.screenshot({
      path: join(process.env.SCAT_VIS_QA_DIR, `p4-${info.project.name}.png`),
      fullPage: false,
    })
    await page
      .getByLabel('已审核自主图表')
      .first()
      .screenshot({ path: join(process.env.SCAT_VIS_QA_DIR, `p4-card-${info.project.name}.png`) })
  }
})

test('manual creation recovers the same unknown request and supports cancellation', async ({
  page,
}) => {
  const requests: object[] = []
  let state = ''
  await page.addInitScript(
    ({ thread }) =>
      sessionStorage.setItem(
        'scat-hermes-conversation-v1:visualization-qa',
        JSON.stringify({ threadId: thread, messages: [] }),
      ),
    { thread },
  )
  await page.route('**/portal-session', (route) =>
    route.fulfill({ json: { username: 'qa', owner_id: 'visualization-qa', role: 'user' } }),
  )
  const task = () => ({
    task_id: first,
    state,
    phase: 'queued',
    intent: '将当前采样页画成两个子图',
    created_at: 1,
    version: 1,
  })
  await page.route('**/api/v1/hermes/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    if (path.endsWith('/visualization-tasks'))
      return route.fulfill({ json: { enabled: true, items: state ? [task()] : [] } })
    if (path.endsWith('/visualization-sources'))
      return route.fulfill({
        json: {
          items: [
            {
              kind: 'readonly_result',
              source_id: artifact,
              run_id: 'run_fixture',
              payload_sha256: sha,
              operation: 'get_scat_track_points',
            },
          ],
        },
      })
    if (path.endsWith('/cancel')) {
      state = 'cancelled'
      return route.fulfill({ json: task() })
    }
    if (path.endsWith('/visualizations')) {
      requests.push(route.request().postDataJSON())
      state = 'queued'
      return requests.length === 1
        ? route.fulfill({ status: 503, json: {} })
        : route.fulfill({ status: 202, json: task() })
    }
    return route.fulfill({ json: { items: [] } })
  })
  await page.goto('/assistant')
  await page.getByRole('button', { name: '根据会话数据绘图' }).click()
  await page.getByLabel('自主绘图要求').fill('将当前采样页画成两个子图')
  await page.getByRole('button', { name: '创建图表', exact: true }).click()
  await expect(
    page.getByText('入队结果尚未确认。请先刷新任务；恢复请求会使用原请求号。'),
  ).toBeVisible()
  expect(requests).toHaveLength(1)
  await page.reload()
  await expect(page.getByRole('button', { name: '恢复原绘图请求' })).toBeVisible()
  expect(requests).toHaveLength(1)
  await page.getByRole('button', { name: '刷新图表任务' }).click()
  await expect(page.getByText('版本 1 · 排队中')).toBeVisible()
  await page.getByRole('button', { name: '恢复原绘图请求' }).click()
  await expect(page.getByRole('button', { name: '恢复原绘图请求' })).toHaveCount(0)
  expect(requests).toHaveLength(2)
  expect(requests[0]).toEqual(requests[1])
  await page.getByRole('button', { name: '取消绘图' }).click()
  await expect(page.getByText('版本 1 · 已取消')).toBeVisible()
  await expect(page.getByAltText('已审核图表预览')).toHaveCount(0)
})
