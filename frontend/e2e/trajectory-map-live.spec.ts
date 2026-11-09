import { expect, test } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.skip(process.env.SCAT_MAP_LIVE !== '1', 'Explicit GET-only live map validation')

test('saved real result locates both maps and supports fullscreen without model calls', async ({
  page,
}, info) => {
  let writes = 0,
    auth = 0,
    tiles = 0
  const errors: string[] = []
  let center: { longitude: number; latitude: number } | undefined
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('request', (request) => {
    if (request.headers().authorization) auth++
  })
  page.on('response', async (response) => {
    if (
      response.url().startsWith('https://tile.openstreetmap.org/') &&
      response.status() === 200 &&
      response.headers()['content-type']?.startsWith('image/')
    )
      tiles++
    if (!/\/results\/[^/]+$/.test(new URL(response.url()).pathname)) return
    const body = await response.json().catch(() => null)
    const block = body?.analysis_result?.blocks?.find(
      (b: { type: string }) => b.type === 'trajectory_2d',
    )
    if (!block?.points?.length) return
    const points = block.points as Record<string, number>[]
    const xs = points.map((p) => Number(p[block.x_key])),
      ys = points.map((p) => Number(p[block.y_key]))
    center = {
      longitude: (Math.min(...xs) + Math.max(...xs)) / 2,
      latitude: (Math.min(...ys) + Math.max(...ys)) / 2,
    }
  })
  await page.route('**/api/v1/**', (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      writes++
      return route.abort()
    }
    return route.continue()
  })
  await page.goto('/?thread_id=343b8430-55d0-40bf-b6e1-cb0b4ca65f3c')
  await page
    .getByRole('button', { name: /查看来源/ })
    .first()
    .click()
  await page
    .locator('.evidence-item')

    .getByRole('button', { name: '查看数据', exact: true })
    .click()
  const map = page.getByRole('region', { name: '二维街道轨迹地图' })
  const scene = map.locator('.cesium-container')
  const centered = async (target: typeof scene) => {
    await expect.poll(() => Boolean(center)).toBe(true)
    expect(
      Math.abs(Number(await target.getAttribute('data-camera-longitude')) - center!.longitude),
    ).toBeLessThan(1)
    expect(
      Math.abs(Number(await target.getAttribute('data-camera-latitude')) - center!.latitude),
    ).toBeLessThan(1)
  }
  const fullscreen = async (region: typeof map) => {
    await region.getByRole('button', { name: '全屏', exact: true }).click()
    await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(true)
    await expect
      .poll(async () => {
        const canvas = await region.locator('canvas').first().boundingBox()
        return Boolean(canvas && canvas.width > 200 && canvas.height > 200)
      })
      .toBe(true)
    await region.getByRole('button', { name: '退出全屏', exact: true }).click()
    await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(false)
  }
  await expect(scene).toHaveAttribute('data-point-count', '100')
  await expect(scene).toHaveAttribute('data-scene-dimension', '2d')
  await centered(scene)
  await expect.poll(() => tiles, { timeout: 30000 }).toBeGreaterThan(0)
  await expect(map.getByText('OpenStreetMap contributors', { exact: true })).toBeVisible()
  await fullscreen(map)
  await page.getByRole('button', { name: '打开轨迹交互' }).click()
  const workspace = page.getByRole('region', { name: '轨迹交互工作台', exact: true })
  await workspace.getByRole('button', { name: '切换三维' }).click()
  const globe = workspace.locator('.cesium-container')
  await expect(globe).toHaveAttribute('data-point-count', '100')
  await expect(globe).toHaveAttribute('data-scene-dimension', '3d')
  await centered(globe)
  await expect(workspace.getByTestId('playback-toggle')).toHaveCount(0)
  await expect(workspace.getByLabel('当前姿态估计')).toHaveCount(0)
  await workspace.getByRole('button', { name: '定位轨迹', exact: true }).click()
  await centered(globe)
  await fullscreen(workspace)
  expect(writes).toBe(0)
  expect(auth).toBe(0)
  expect(errors).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  const folder = resolve(import.meta.dirname, '../../var/validation/20261107-map-view-repair')
  mkdirSync(folder, { recursive: true })
  writeFileSync(
    resolve(folder, `live-${info.project.name}.json`),
    JSON.stringify(
      {
        status: 'passed',
        writes,
        auth,
        successful_street_tiles: tiles,
        displayed_points: 100,
        default_streets: true,
        actual_camera_centered_2d_3d: true,
        fullscreen_2d_3d: true,
        raw_data_saved: false,
        model_calls: 0,
        screenshots: false,
      },
      null,
      2,
    ),
  )
})
