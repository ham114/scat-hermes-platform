import { expect, test } from '@playwright/test'

test('exact future window is reviewable before confirmation and restores without executing', async ({
  page,
}) => {
  const id = '76eaee86-33af-4f15-ac1c-21ba989ae660'
  let writes = 0
  await page.addInitScript(
    (threadId) =>
      sessionStorage.setItem(
        'scat-hermes-conversation-v1',
        JSON.stringify({ threadId, messages: [] }),
      ),
    id,
  )
  await page.route('**/api/v1/hermes/**', async (route) => {
    if (!['GET', 'HEAD'].includes(route.request().method())) {
      writes++
      return route.abort()
    }
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/model-tasks'))
      return route.fulfill({
        json: {
          items: [
            {
              task_id: 'a'.repeat(32),
              run_id: 'b'.repeat(32),
              state: 'pending',
              snapshot_sha256: 'c'.repeat(64),
              expires_at: 9999999999,
              model: {
                display_name: '未来轨迹预测',
                model_key: 'scat-workflow-forecast',
                version: '1',
                limits: { trials: 1 },
              },
              input_refs: [{ batch: 'synthetic', external_id: 'forecast-1' }],
              parameters: { observation_last_n: 200 },
              evaluator: null,
              components: {},
              code_version: 'test',
              workflow: {
                proposal: { kind: 'forecast' },
                coverage: { selected_count: 1 },
                trials: [{ trial_id: 'one' }],
                synthetic: true,
                tool_groups: {},
                input_sha256: 'd'.repeat(64),
                prediction_contract: {
                  mode: 'future_forecast',
                  observation_last_n: 200,
                  windows: [
                    {
                      external_id: 'forecast-1',
                      point_count: 200,
                      start_time: '2020-01-01T00:00:00Z',
                      forecast_origin: '2020-01-01T00:15:00Z',
                      span_s: 900,
                    },
                  ],
                },
              },
            },
          ],
        },
      })
    return route.fulfill({ json: { items: [] } })
  })
  await page.goto('/assistant')
  await expect(page.getByText('从末次观测向未来预测', { exact: false })).toBeVisible()
  await expect(
    page.getByText('预测起点之后没有已知真值，不计算误差指标。', { exact: true }),
  ).toBeVisible()
  await expect(page.getByText('航班 forecast-1', { exact: false })).toContainText('200 点')
  await expect(page.getByText('航班 forecast-1', { exact: false })).toContainText(
    '2020-01-01T00:15:00Z',
  )
  await expect(page.getByRole('button', { name: '确认参数并执行' })).toBeVisible()
  await page.reload()
  await expect(page.getByText('从末次观测向未来预测', { exact: false })).toBeVisible()
  expect(writes).toBe(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})
