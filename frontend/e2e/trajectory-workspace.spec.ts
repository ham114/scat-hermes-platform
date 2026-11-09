import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import fixture from '../src/test-fixtures/analysis.json' with { type: 'json' }
test('geographic result keeps 2D playback, profile cursor and real Cesium controls', async ({
  page,
}, info) => {
  test.setTimeout(60000)
  const thread = '3d305763-d908-45c4-9692-c994a3cb5372',
    task = 'a'.repeat(32)
  const payload = structuredClone(fixture)
  const block = payload.analysis_result.blocks.find((x) => x.type === 'trajectory_2d')!
  block.x_unit = 'degree'
  block.y_unit = 'degree'
  block.x_label = '经度'
  block.y_label = '纬度'
  for (const point of block.points!) {
    point.east = 116 + Number(point.east) / 1000
    point.north = 39 + Number(point.north) / 1000
  }
  const last = block.points!.at(-1)!
  block.points!.push({
    ...last,
    t: Number(last.t) + 10,
    east: Number(last.east) + 0.01,
    north: Number(last.north) + 0.01,
  })
  let writes = 0
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.addInitScript(
    ({ thread }) =>
      sessionStorage.setItem(
        'scat-hermes-conversation-v1',
        JSON.stringify({ threadId: thread, messages: [] }),
      ),
    { thread },
  )
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.hostname !== '127.0.0.1') return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (route.request().method() !== 'GET') writes++
    if (url.pathname.endsWith('/model-tasks'))
      return route.fulfill({
        json: {
          items: [
            {
              task_id: task,
              run_id: payload.run_id,
              thread_id: thread,
              state: 'finished',
              model: {
                display_name: '合成地理模型',
                version: '1',
                model_key: 'synthetic',
                limits: {},
              },
              run: { status: 'succeeded', progress: 1, result_artifact_ids: [payload.artifact_id] },
              expires_at: 9999999999,
              input_refs: [],
              components: {},
              snapshot_sha256: 'd'.repeat(64),
              parameters: {},
              progress: {},
              artifacts: [{ artifact_id: payload.artifact_id, kind: 'analysis_result' }],
            },
          ],
        },
      })
    if (url.pathname.endsWith('/artifacts/' + payload.artifact_id))
      return route.fulfill({ json: payload })
    if (url.pathname.endsWith('/workflow-catalog'))
      return route.fulfill({ json: { enabled: false, groups: {}, model_ids: [] } })
    return route.fulfill({ json: { items: [], next_cursor: null } })
  })
  await page.goto('/assistant')
  await page.getByRole('button', { name: '查看分析结果' }).click()
  await page.getByRole('button', { name: '打开轨迹交互' }).click()
  const workspace = page.getByRole('region', { name: '轨迹交互工作台', exact: true })
  await expect(workspace.getByTestId('playback-toggle')).toBeVisible()
  await workspace.getByTestId('playback-toggle').click()
  await expect(workspace.getByTestId('playback-toggle')).toContainText('暂停')
  await workspace.getByTestId('playback-toggle').click()
  await workspace.getByLabel('交互快照').selectOption({ label: '时刻B' })
  await workspace.getByLabel('交互快照').selectOption({ label: '时刻A' })
  await workspace.getByRole('button', { name: '切换三维' }).click()
  await expect(workspace.locator('.cesium-widget canvas')).toBeVisible({ timeout: 30000 })
  await workspace.getByRole('button', { name: '跟随视角', exact: true }).click()
  await expect(workspace.getByRole('button', { name: '跟随中', exact: true })).toBeVisible()
  await workspace.locator('.view-preset-select .el-select__wrapper').click()
  await page.getByRole('option', { name: '正上方俯视', exact: true }).click()
  await workspace.locator('.altitude-scale-select .el-select__wrapper').click()
  await page.getByRole('option', { name: '高度 2×', exact: true }).click()
  await expect(workspace.getByLabel('当前姿态估计')).toBeVisible()
  expect(errors).toEqual([])
  expect(writes).toBe(0)
  await workspace.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261106-trajectory-${info.project.name}.png`,
    ),
  })
  await workspace.getByRole('button', { name: '切换二维' }).click()
  await expect(workspace.locator('.cesium-container[data-scene-dimension="2d"]')).toBeVisible()
  await workspace.getByRole('button', { name: '经纬度坐标图' }).click()
  await expect(workspace.getByRole('region', { name: '二维轨迹时间轴回放' })).toBeVisible()
})
