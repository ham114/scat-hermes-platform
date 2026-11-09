import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
test.skip(process.env.SCAT_WRITABLE_LIVE !== '1', 'Opt-in existing-result GETs only')
test('existing real results stay readable on mobile and UI status is localized without writes', async ({
  page,
}) => {
  const folder = resolve(import.meta.dirname, '../../var/validation/20261107-writable-live')
  const success = JSON.parse(readFileSync(resolve(folder, 'browser-live-receipt.json'), 'utf8'))
  const controls = JSON.parse(
    readFileSync(resolve(folder, 'browser-controls-receipt.json'), 'utf8'),
  )
  let writes = 0
  page.on('request', (r) => {
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(r.method())) writes++
  })
  await page.goto('/assistant?thread_id=' + success.thread_id)
  await page.getByRole('button', { name: '查看分析结果', exact: true }).first().click()
  const panel = page.getByRole('region', { name: '结构化分析结果' })
  await expect(panel).toBeVisible()
  await page.setViewportSize({ width: 393, height: 851 })
  const table = panel.locator('table').nth(0)
  expect(
    await table
      .locator('td')
      .first()
      .evaluate((e) => getComputedStyle(e).whiteSpace),
  ).toBe('nowrap')
  expect(
    await panel
      .locator('.result-scroll')
      .evaluateAll((nodes) => nodes.some((e) => e.scrollWidth > e.clientWidth)),
  ).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      '../../docs/validation/20261107-writable-result-mobile-final.png',
    ),
    fullPage: true,
    scale: 'css',
  })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      '../../docs/validation/20261107-writable-result-desktop-final.png',
    ),
    fullPage: true,
    scale: 'css',
  })
  await page.goto('/assistant?thread_id=' + controls.thread_id)
  await expect(page.getByText('已取消', { exact: true })).toBeVisible()
  await expect(page.getByText('已撤销', { exact: true })).toBeVisible()
  expect(writes).toBe(0)
  await page.screenshot({
    path: resolve(import.meta.dirname, '../../docs/validation/20261107-writable-cancel-final.png'),
    fullPage: true,
    scale: 'css',
  })
  writeFileSync(
    resolve(folder, 'readback-layout.json'),
    JSON.stringify(
      { status: 'passed', post_count: 0, mobile_table_scroll: true, revoked_localized: true },
      null,
      2,
    ),
  )
})
