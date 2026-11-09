import { expect, test } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.skip(process.env.SCAT_RECEIPT_LIVE !== '1', 'Explicit receipt diagnosis only')
const folder = resolve(import.meta.dirname, '../../var/validation/20261107-receipt-diagnosis')
function save(name: string, value: object) {
  mkdirSync(folder, { recursive: true })
  writeFileSync(resolve(folder, name + '.json'), JSON.stringify(value, null, 2))
}

test('failed saved instructions explain empty sources while an older real receipt still renders', async ({
  page,
}, info) => {
  let writes = 0
  await page.route('**/api/v1/**', (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      writes++
      return route.abort()
    }
    return route.continue()
  })
  await page.goto('/?thread_id=343b8430-55d0-40bf-b6e1-cb0b4ca65f3c')
  const buttons = page.getByRole('button', { name: /查看来源/ })
  await expect(buttons).toHaveCount(5)
  await buttons.last().click()
  await expect(
    page.getByText(
      '本次指令执行失败，没有可读取的只读数据。此前成功查询的数据仍可从对应历史回复读取。',
      { exact: true },
    ),
  ).toBeVisible()
  await buttons.first().click()
  await page
    .locator('.evidence-item')
    .first()
    .getByRole('button', { name: '查看数据', exact: true })
    .click()
  await expect(
    page.getByRole('region', { name: '二维街道轨迹地图' }).locator('.cesium-container'),
  ).toHaveAttribute('data-point-count', '100')
  expect(writes).toBe(0)
  save('readback-' + info.project.name, {
    status: 'passed',
    writes,
    failed_source_explained: true,
    old_receipt_points: 100,
    raw_data_saved: false,
  })
})

test('one ordinary plotting request checks current provider recovery', async ({ page }, info) => {
  test.skip(
    process.env.SCAT_RECEIPT_SEND !== '1' || info.project.name !== 'desktop',
    'Single explicit check; never retry automatically',
  )
  let tid = '',
    rid = '',
    posts = 0,
    predictionWrites = 0
  page.on('response', async (response) => {
    if (response.request().method() !== 'POST') return
    const path = new URL(response.url()).pathname
    const body = await response.json().catch(() => null)
    if (path.endsWith('/threads')) tid = body?.thread_id ?? tid
    if (path.endsWith('/runs')) rid = body?.run_id ?? rid
    save('recovery-run', { thread_id: tid, run_id: rid, status: 'running', posts })
  })
  await page.route('**/api/v1/**', (route) => {
    const request = route.request()
    if (request.method() === 'POST') {
      posts++
      if (!/\/hermes\/threads(?:\/[a-f0-9-]+\/runs)?$/.test(new URL(request.url()).pathname)) {
        predictionWrites++
        return route.abort()
      }
    }
    return route.continue()
  })
  await page.goto('/')
  await page
    .getByLabel('你的问题')
    .fill('在SCAT中查找ID为100004的记录，绘制它的二维轨迹图，并简要分析运动特征。')
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(page.getByText(/^(已完成|执行失败|对话已中断)$/)).toBeVisible({ timeout: 210000 })
  const status = await page.getByText(/^(已完成|执行失败|对话已中断)$/).innerText()
  save('recovery-run', {
    thread_id: tid,
    run_id: rid,
    status,
    posts,
    prediction_writes: predictionWrites,
    raw_data_saved: false,
  })
  expect(status).toBe('已完成')
  await page.getByRole('button', { name: /查看来源/ }).click()
  const receipt = page.locator('.evidence-item')
  await receipt.first().getByRole('button', { name: '查看数据', exact: true }).click()
  await expect(
    page.getByRole('region', { name: '二维街道轨迹地图' }).locator('.cesium-container'),
  ).toHaveAttribute('data-track-count', '1')
  expect(posts).toBe(2)
  expect(predictionWrites).toBe(0)
  save('recovery-run', {
    thread_id: tid,
    run_id: rid,
    status: 'passed',
    posts,
    prediction_writes: predictionWrites,
    actual_track_receipt_rendered: true,
    raw_data_saved: false,
  })
})
