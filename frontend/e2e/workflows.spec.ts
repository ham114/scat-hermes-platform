import { expect, test } from '@playwright/test'
import { resolve } from 'node:path'

const threadId = '76eaee86-33af-4f15-ac1c-21ba989ae660'
const groups = {
  scat_data_steps: ['scat_sample_prepare'],
  scat_materialization_steps: ['scat_materialize', 'scat_materialization_read'],
  scat_experiment_steps: ['scat_trial_run'],
  scat_evaluation_steps: ['scat_evaluation_summarize', 'scat_evaluation_diagnose'],
  scat_report_steps: ['scat_report_build', 'scat_report_read'],
}

test('workflow groups, durable steps and partial results are visible without new execution', async ({
  page,
}, info) => {
  let posts = 0
  await page.addInitScript(
    (id) =>
      sessionStorage.setItem(
        'scat-hermes-conversation-v1',
        JSON.stringify({ threadId: id, messages: [] }),
      ),
    threadId,
  )
  await page.route('**/api/v1/hermes/**', async (route) => {
    if (route.request().method() === 'POST') posts++
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/workflow-catalog'))
      return route.fulfill({
        json: {
          enabled: true,
          synthetic: true,
          groups,
          control_group: { scat_workflows: ['scat_workflow_prepare'] },
          model_ids: ['constant-velocity-heading', 'kalman-cv'],
          production_gpu: false,
        },
      })
    if (path.endsWith('/model-tasks'))
      return route.fulfill({
        json: {
          items: [
            {
              task_id: 'a'.repeat(32),
              run_id: 'b'.repeat(32),
              state: 'finished',
              snapshot_sha256: 'c'.repeat(64),
              expires_at: 9999999999,
              input_refs: [{ batch: 'SYNTH-S5', external_id: 'SYNTH-S5-001' }],
              parameters: {},
              evaluator: { key: 'm6-eval', version: '1' },
              components: {},
              code_version: 'skills-s5-v1',
              model: {
                display_name: '合成双模型比较',
                model_key: 'scat-workflow-comparison',
                version: '1',
                limits: { trials: 2 },
              },
              workflow: {
                proposal: { kind: 'comparison', failure_policy: 'keep_partial' },
                coverage: { selected_count: 2, candidate_count: 3 },
                trials: [{ trial_id: 'one' }, { trial_id: 'two' }],
                synthetic: true,
                tool_groups: groups,
                input_sha256: 'd'.repeat(64),
              },
              skill_job: {
                state: 'partial',
                budget: { llm_calls: 9, tool_calls: 16, charged_tokens: 12000 },
                steps: [
                  { operation: 'scat_sample_prepare', state: 'succeeded' },
                  { operation: 'scat_trial_run', state: 'succeeded' },
                  { operation: 'scat_report_build', state: 'succeeded' },
                ],
                backend_executions: [
                  { id: 'one', state: 'succeeded', calls: 1 },
                  { id: 'two', state: 'succeeded', calls: 1 },
                ],
              },
              run: {
                status: 'partial',
                result_artifact_ids: ['skill-result-example'],
                progress: 1,
              },
            },
          ],
        },
      })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/assistant')
  await page.getByRole('button', { name: '流程模板' }).click()
  await page.getByText('技能与工具组 · 复杂流程已启用', { exact: true }).click()
  await expect(page.getByRole('heading', { name: '模型与实验', exact: true })).toBeVisible()
  await expect(page.getByText('部分成功', { exact: true })).toBeVisible()
  await page.getByText('持久步骤与累计预算', { exact: true }).click()
  await expect(page.getByText('执行已批准的模型推理 · 执行成功', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '下载完整结果 JSON' })).toBeVisible()
  await expect(page.getByRole('button', { name: '确认参数并执行' })).toHaveCount(0)
  expect(posts).toBe(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/${process.env.SCAT_E2E_SCREENSHOT_PREFIX ?? '20261023-S5-workflow'}-workflows-${info.project.name}.png`,
    ),
    fullPage: true,
  })
})
