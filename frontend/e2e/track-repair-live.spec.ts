import { expect, test } from '@playwright/test'
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.skip(process.env.SCAT_TRACK_REPAIR_LIVE !== '1', 'Explicit live repair validation only')
const folder = resolve(import.meta.dirname, '../../var/validation/20261107-track-drawing-failure')

test('failed real run keeps its status while its successful track receipts render', async ({
  page,
}, info) => {
  let writes = 0
  let auth = 0
  page.on('request', (request) => {
    if (request.headers().authorization) auth++
  })
  await page.route('**/api/v1/**', (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      writes++
      return route.abort()
    }
    return route.continue()
  })
  const tid = '3a298bec-bafd-48bd-b99c-509f295dbd1f'
  await page.goto('/?thread_id=' + tid)
  await expect(page.getByText('执行失败', { exact: true }).first()).toBeVisible()
  await page
    .getByRole('button', { name: /查看来源/ })
    .first()
    .click()
  const tracks = page.locator('.evidence-item')
  await expect(tracks).toHaveCount(1)
  await tracks.first().getByRole('button', { name: '查看数据', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'SCAT 二维轨迹', exact: true })).toBeVisible()
  await expect(page.locator('.analysis-result canvas').first()).toBeVisible()
  await expect(page.locator('.analysis-result')).toContainText('524')
  await expect(page.locator('.cesium-container')).toHaveAttribute('data-point-count', '524')
  await page.reload()
  await expect(page.getByText('执行失败', { exact: true }).first()).toBeVisible()
  expect(writes).toBe(0)
  expect(auth).toBe(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  mkdirSync(folder, { recursive: true })
  writeFileSync(
    resolve(folder, `readback-${info.project.name}.json`),
    JSON.stringify(
      {
        status: 'passed',
        writes,
        auth,
        historical_status: 'failed',
        track_pages: 6,
        source_total: 524,
        merged_full_track_chart: true,
        raw_data_saved: false,
      },
      null,
      2,
    ),
  )
})

test('new ordinary plotting request produces a short answer and a real page chart', async ({
  page,
}, info) => {
  test.skip(process.env.SCAT_TRACK_REPAIR_SEND !== '1', 'Explicit single model call chain only')
  test.skip(
    info.project.name !== 'desktop',
    'Send once; mobile validation reads the same saved result',
  )
  let posts = 0
  let auth = 0
  let tid = ''
  let rid = ''
  page.on('request', (request) => {
    if (request.method() === 'POST') posts++
    if (request.headers().authorization) auth++
  })
  page.on('response', async (response) => {
    if (response.request().method() !== 'POST') return
    const path = new URL(response.url()).pathname
    const body = await response.json().catch(() => null)
    if (path.endsWith('/threads')) tid = body?.thread_id ?? tid
    if (path.endsWith('/runs')) rid = body?.run_id ?? rid
    mkdirSync(folder, { recursive: true })
    writeFileSync(
      resolve(folder, 'live-run.json'),
      JSON.stringify({ thread_id: tid, run_id: rid, status: 'running' }),
    )
  })
  await page.goto('/')
  await page.getByLabel('你的问题').fill('在SCAT中查找ID为100002的记录，绘制下它的二维轨迹图')
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(page.getByText('已完成', { exact: true })).toBeVisible({ timeout: 210000 })
  const answer = page.locator('.assistant-message.assistant .message-content > p')
  expect((await answer.innerText()).length).toBeLessThan(1200)
  await page
    .getByRole('button', { name: /查看来源/ })
    .first()
    .click()
  const tracks = page.locator('.evidence-item')
  await expect(tracks).toHaveCount(1)
  await tracks.getByRole('button', { name: '查看数据', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'SCAT 二维轨迹', exact: true })).toBeVisible()
  expect(posts).toBe(2)
  expect(auth).toBe(0)
  writeFileSync(
    resolve(folder, 'live-run.json'),
    JSON.stringify(
      {
        thread_id: tid,
        run_id: rid,
        status: 'passed',
        posts,
        auth,
        track_pages: 1,
        raw_data_saved: false,
        ordinary_prompt: true,
      },
      null,
      2,
    ),
  )
})

test('mobile reopens the new result without running the model again', async ({ page }, info) => {
  test.skip(info.project.name !== 'mobile', 'Mobile GET readback only')
  test.skip(process.env.SCAT_TRACK_REPAIR_SEND !== '1', 'Uses the single desktop result')
  const previous = JSON.parse(readFileSync(resolve(folder, 'live-run.json'), 'utf8'))
  expect(previous.status).toBe('passed')
  let writes = 0
  await page.route('**/api/v1/**', (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      writes++
      return route.abort()
    }
    return route.continue()
  })
  await page.goto('/?thread_id=' + previous.thread_id)
  await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  await page
    .getByRole('button', { name: /查看来源/ })
    .first()
    .click()
  await page
    .locator('.evidence-item')

    .getByRole('button', { name: '查看数据', exact: true })
    .click()
  await expect(page.getByRole('heading', { name: 'SCAT 二维轨迹', exact: true })).toBeVisible()
  expect(writes).toBe(0)
  writeFileSync(
    resolve(folder, 'new-result-mobile.json'),
    JSON.stringify({ status: 'passed', writes }),
  )
})
