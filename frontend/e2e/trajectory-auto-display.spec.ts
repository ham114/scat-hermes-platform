import { test, expect } from '@playwright/test'
import fixture from '../src/test-fixtures/readonly-track.json' with { type: 'json' }
test('explicit plotting automatically shows the bounded full view and refreshes without a new run', async ({
  page,
}) => {
  const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55',
    rid = 'run_auto_view'
  const seed = {
    result_id: fixture.result_id,
    payload_sha256: fixture.payload_sha256,
    operation: fixture.operation,
    created_at: 1,
    expires_at: 9999999999,
    kind: 'readonly_tool_result',
    trajectory_view_available: true,
  }
  const display = { ...seed, result_id: 'c'.repeat(32), payload_sha256: 'd'.repeat(64) }
  const analysis = structuredClone(fixture.analysis_result)
  const block = analysis.blocks.find((b) => b.type === 'trajectory_2d')!
  delete block.time_key
  block.points.push({ ...block.points[1]!, longitude: 4.04, latitude: 50.02 })
  analysis.blocks = [block]
  Object.assign(analysis.context, {
    readonly_result_id: display.result_id,
    readonly_payload_sha256: display.payload_sha256,
  })
  let writes = 0,
    viewReads = 0
  await page.route('https://tile.openstreetmap.org/**', (r) => r.abort())
  await page.route('**/api/v1/**', (r) => {
    if (r.request().method() !== 'GET') writes++
    const path = new URL(r.request().url()).pathname
    if (path.endsWith('/messages'))
      return r.fulfill({
        json: {
          items: [
            {
              id: 1,
              request_id: tid,
              run_id: rid,
              input: '把它的二维轨迹图画出来。',
              input_available: true,
              output: '数据范围（系统核验）：分析依据是已读取的2点。',
              status: 'completed',
              created_at: 1,
              updated_at: 2,
              last_sequence: 0,
            },
          ],
          next_cursor: null,
        },
      })
    if (path.endsWith('/results')) return r.fulfill({ json: { items: [seed] } })
    if (path.endsWith('/trajectory')) {
      viewReads++
      return r.fulfill({
        json: {
          items: [display],
          coverage: {
            total: 3,
            loaded: 3,
            every_nth: 1,
            complete: true,
            full_extent: true,
            partial: false,
          },
        },
      })
    }
    if (path.endsWith('/results/' + seed.result_id))
      return r.fulfill({ json: { ...fixture, ...seed } })
    if (path.endsWith('/results/' + display.result_id))
      return r.fulfill({
        json: {
          ...display,
          result: { ...fixture.result, display_only: true },
          analysis_result: analysis,
        },
      })
    if (path.endsWith('/model-tasks')) return r.fulfill({ json: { items: [] } })
    return r.fulfill({ status: 404, json: {} })
  })
  await page.goto('/?thread_id=' + tid)
  const main = page.getByRole('region', { name: '本轮主结果', exact: true })
  await expect(main.locator('.cesium-container')).toHaveAttribute('data-point-count', '3')
  await expect(page.getByText(/地图展示 3\/3 点/)).toBeVisible()
  expect(await page.locator('details.source-details').getAttribute('open')).toBeNull()
  await page.reload()
  await expect(main.locator('.cesium-container')).toHaveAttribute('data-point-count', '3')
  expect(viewReads).toBe(2)
  expect(writes).toBe(0)
})
