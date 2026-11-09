import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import fixture from '../src/test-fixtures/readonly-track.json' with { type: 'json' }

test('untimed geographic result defaults to streets, locates 3D, and supports fullscreen', async ({
  page,
}, info) => {
  test.setTimeout(60000)
  const tid = 'a9322722-e12b-4ed1-a2da-180d91056a55',
    rid = 'run_static_map'
  const source = structuredClone(fixture)
  const block = source.analysis_result.blocks.find((b) => b.type === 'trajectory_2d')!
  delete block.time_key
  const errors: string[] = []
  let streetRequests = 0,
    writes = 0
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('https://tile.openstreetmap.org/**', (route) => {
    streetRequests++
    if (process.env.SCAT_MAP_TILES_ONLINE === '1') return route.continue()
    // Deterministic tile only for WebGL tests, not proof of the public service.
    return route.fulfill({
      contentType: 'image/png',
      headers: { 'Access-Control-Allow-Origin': '*' },
      body: Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=',
        'base64',
      ),
    })
  })
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
              input: '查看轨迹',
              input_available: true,
              output: '合成地图测试',
              status: 'completed',
              last_sequence: 0,
            },
          ],
          next_cursor: null,
        },
      })
    if (path.endsWith('/results'))
      return route.fulfill({
        json: {
          items: [
            {
              result_id: source.result_id,
              operation: source.operation,
              payload_sha256: source.payload_sha256,
              created_at: 1,
              expires_at: 9999999999,
              kind: 'readonly_tool_result',
            },
          ],
        },
      })
    if (path.endsWith('/results/' + source.result_id))
      return route.fulfill({
        json: {
          ...source,
          created_at: 1,
          expires_at: 9999999999,
          kind: 'readonly_tool_result',
        },
      })
    if (path.endsWith('/model-tasks')) return route.fulfill({ json: { items: [] } })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/?thread_id=' + tid)
  await page.getByRole('button', { name: /查看来源/ }).click()
  await page.getByRole('button', { name: '查看数据', exact: true }).click()
  const map = page.getByRole('region', { name: '二维街道轨迹地图' })
  const scene = map.locator('.cesium-container')
  await expect(scene).toHaveAttribute('data-point-count', '2')
  await expect(scene).toHaveAttribute('data-scene-dimension', '2d')
  await expect.poll(() => streetRequests).toBeGreaterThan(0)
  await expect(map.getByText('OpenStreetMap contributors', { exact: true })).toBeVisible()
  const centered = async (target: typeof scene) => {
    const lon = Number(await target.getAttribute('data-camera-longitude'))
    const lat = Number(await target.getAttribute('data-camera-latitude'))
    expect(Math.abs(lon - 4.01)).toBeLessThan(1)
    expect(Math.abs(lat - 50.005)).toBeLessThan(1)
  }
  await centered(scene)
  await map.getByRole('button', { name: '全屏', exact: true }).click()
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(true)
  await map.getByRole('button', { name: '退出全屏', exact: true }).click()
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(false)
  await page.getByRole('button', { name: '打开轨迹交互' }).click()
  const workspace = page.getByRole('region', { name: '轨迹交互工作台', exact: true })
  await expect(workspace.getByTestId('playback-toggle')).toHaveCount(0)
  await workspace.getByRole('button', { name: '切换三维' }).click()
  const globe = workspace.locator('.cesium-container')
  await expect(globe).toHaveAttribute('data-point-count', '2')
  await expect(globe).toHaveAttribute('data-scene-dimension', '3d')
  await centered(globe)
  await expect(workspace.getByLabel('当前姿态估计')).toHaveCount(0)
  await workspace.getByRole('button', { name: '定位轨迹', exact: true }).click()
  await centered(globe)
  await workspace.getByRole('button', { name: '全屏', exact: true }).click()
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(true)
  await workspace.getByRole('button', { name: '退出全屏', exact: true }).click()
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(false)
  // Let the fullscreen canvas resize and online tiles finish before visual QA.
  await page.waitForTimeout(1500)
  await workspace.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261107-map-view-${info.project.name}.png`,
    ),
  })
  expect(errors).toEqual([])
  expect(writes).toBe(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
