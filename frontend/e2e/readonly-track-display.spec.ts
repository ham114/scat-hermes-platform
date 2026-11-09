import { expect, test } from '@playwright/test'
import { resolve } from 'node:path'
import fixture from '../src/test-fixtures/readonly-track.json' with { type: 'json' }

test('historical readonly track renders a chart and reopens without writes', async ({
  page,
}, info) => {
  const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55'
  const rid = 'run_readonly_track'
  const reference = {
    result_id: fixture.result_id,
    payload_sha256: fixture.payload_sha256,
    operation: fixture.operation,
    created_at: 1,
    expires_at: 9999999999,
    kind: 'readonly_tool_result',
  }
  let writes = 0
  await page.route('**/api/v1/hermes/**', (route) => {
    if (route.request().method() !== 'GET') writes++
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/messages'))
      return route.fulfill({
        json: {
          items: [
            {
              id: 1,
              request_id: tid,
              run_id: rid,
              input: '绘制它的二维轨迹图',
              input_available: true,
              output: '合成轨迹回执，可查看图表。',
              status: 'completed',
              created_at: 1,
              updated_at: 2,
              last_sequence: 0,
            },
          ],
          next_cursor: null,
        },
      })
    if (path.endsWith('/runs/' + rid))
      return route.fulfill({
        json: {
          run_id: rid,
          status: 'completed',
          completed: true,
          output: '合成轨迹回执，可查看图表。',
        },
      })
    if (path.endsWith('/results')) return route.fulfill({ json: { items: [reference] } })
    if (path.endsWith('/results/' + reference.result_id))
      return route.fulfill({
        json: {
          ...reference,
          result: fixture.result,
          analysis_result: fixture.analysis_result,
        },
      })
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    return route.fulfill({ status: 404, json: {} })
  })
  const open = async () => {
    await page.getByRole('button', { name: /查看来源/ }).click()
    await page.getByRole('button', { name: '查看数据', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'SCAT 二维轨迹', exact: true })).toBeVisible()
    await expect(page.getByText(/完整范围、点数及是否还有下一页/)).toBeVisible()
    await expect(page.locator('.analysis-result canvas').first()).toBeVisible()
  }
  await page.goto('/?thread_id=' + tid)
  await open()
  await page.getByRole('button', { name: '打开轨迹交互' }).click()
  await expect(page.getByRole('button', { name: '收起轨迹交互' })).toBeVisible()
  await expect(page.locator('.analysis-result')).toContainText('降采样步长')
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261107-readonly-track-${info.project.name}.png`,
    ),
    fullPage: true,
    scale: 'css',
  })
  await page.reload()
  await open()
  expect(writes).toBe(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
