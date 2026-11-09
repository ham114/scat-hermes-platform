import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
test('Honcho memory UI distinguishes queued corrections and confirmed deletion', async ({
  page,
}, info) => {
  let enabled = true,
    epoch = 1,
    deletion = 'none',
    contextReads = 0,
    deleteCalls = 0
  const rows: { request_id: string; content: string; created: number; state: string }[] = []
  await page.route('**/api/v1/hermes/memory**', async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname
    if (path.endsWith('/context')) {
      contextReads++
      return route.fulfill({ json: { enabled, available: true, context: '合成偏好：中文、公里' } })
    }
    if (path.endsWith('/corrections')) {
      if (request.method() === 'POST') {
        const body = request.postDataJSON()
        rows.unshift({ ...body, created: 1794000000, state: 'pending' })
        return route.fulfill({ status: 202, json: { accepted: true, replayed: false } })
      }
      return route.fulfill({ json: { items: rows, limit: 100 } })
    }
    if (path.endsWith('/learning'))
      return route.fulfill({
        json: {
          enabled: true,
          auto_trials: true,
          verified_experiences: 2,
          scheduler_observed: false,
          thresholds: { min_successes: 3, min_distinct_inputs: 2 },
          patterns: [{ pattern: 'synthetic', successes: 2, distinct_inputs: 2 }],
          jobs: [
            {
              id: 'test',
              state: 'ready_for_review',
              created: 1794000000,
              draft: 'a'.repeat(32),
              trial: 'b'.repeat(32),
            },
          ],
        },
      })
    if (request.method() === 'DELETE') {
      deleteCalls++
      enabled = false
      epoch++
      deletion = 'pending'
      rows.length = 0
      return route.fulfill({
        status: 202,
        json: { enabled: false, deletion: 'pending', chat_history_deleted: false },
      })
    }
    if (request.method() === 'PUT') {
      enabled = request.postDataJSON().enabled
      return route.fulfill({ json: { enabled } })
    }
    return route.fulfill({ json: { enabled, epoch, queue: {}, deletion } })
  })
  await page.goto('/memory')
  await expect(page.getByText('记忆已启用', { exact: true })).toBeVisible()
  expect(contextReads).toBe(0)
  await page.getByRole('button', { name: '查看 / 刷新摘要' }).click()
  await expect(page.getByText('合成偏好：中文、公里', { exact: true })).toBeVisible()
  await page.getByLabel('更正内容').fill('请更正，使用米')
  await page.getByRole('button', { name: '提交更正', exact: true }).click()
  await expect(page.getByText(/不代表召回内容已经更新/)).toBeVisible()
  await expect(page.getByText(/待处理 ·/)).toBeVisible()
  await page.getByRole('button', { name: '自动学习', exact: true }).click()
  await expect(page.getByText(/调度进程未核实/)).toBeVisible()
  await expect(page.getByRole('link', { name: '查看草稿与试跑' })).toHaveAttribute(
    'href',
    /draft=aaaaaaaa/,
  )
  await page.getByRole('button', { name: '我的记忆', exact: true }).click()
  expect(deleteCalls).toBe(0)
  await page.getByLabel('确认清空当前用户的长期记忆').check()
  await page.getByRole('button', { name: '清空记忆', exact: true }).click()
  await expect(page.getByText(/清空状态：后台处理中/)).toBeVisible()
  await expect(page.getByRole('button', { name: '启用记忆' })).toBeDisabled()
  deletion = 'completed'
  await page.getByRole('button', { name: '刷新状态' }).click()
  await expect(page.getByText(/清空状态：已确认完成/)).toBeVisible()
  await expect(page.getByRole('status')).toContainText('已确认清空')
  expect(deleteCalls).toBe(1)
  await expect(page.getByText('合成偏好：中文、公里', { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261107-memory-${info.project.name}.png`,
    ),
    fullPage: true,
    scale: 'css',
  })
})
test('agent settings preview saves only pending intent with independent confirmation', async ({
  page,
}, info) => {
  let saved = false,
    writes = 0
  const section = {
    section: 'scat-main',
    value: { model: { default: 'deepseek-flash' }, agent: { temperature: 0.2, max_turns: 16 } },
    config_sha256: 'a'.repeat(64),
    runtime_observed: false,
  }
  await page.route('**/author-api/v1/settings**', async (route) => {
    const r = route.request(),
      p = new URL(r.url()).pathname
    if (p.endsWith('/preview'))
      return route.fulfill({
        json: {
          before: section.value,
          changes: r.postDataJSON().changes,
          application: 'pending_maintenance',
        },
      })
    if (p.endsWith('/proposals')) {
      expect(r.headers()['x-scat-release-key']).toBe('synthetic-confirm')
      writes++
      saved = true
      return route.fulfill({ json: { state: 'pending_maintenance' } })
    }
    return route.fulfill({
      json: {
        sections: [section],
        profiles: [
          {
            name: 'scat-main',
            key_configured: true,
            directories_valid: true,
            state_db_exists: true,
          },
        ],
        usage: {
          day: '2026-11-07',
          today: { requests: 0, reserved_units: 0 },
          lifetime: { requests: 70, reserved_units: 4520033 },
        },
        optional_groups: { 'scat-main': ['scat_workflows'] },
        proposals: saved
          ? [
              {
                id: 'demo',
                section: 'scat-main',
                state: 'pending_maintenance',
                payload: '{"temperature":0.3}',
              },
            ]
          : [],
      },
    })
  })
  await page.goto('/settings')
  await page.getByLabel('设置管理凭据').fill('synthetic-admin')
  await page.getByRole('button', { name: '连接 / 刷新配置' }).click()
  await expect(page.getByRole('status')).toContainText('运行中实际配置尚未核实')
  await page.getByLabel('配置修改JSON').fill('{"temperature":0.3}')
  await page.getByRole('button', { name: '校验并预览差异' }).click()
  expect(writes).toBe(0)
  await expect(page.getByRole('button', { name: '保存待应用修改' })).toBeDisabled()
  await page.getByLabel('确认保存为待维护应用，不立即重启服务').check()
  await page.getByLabel('独立配置确认凭据').fill('synthetic-confirm')
  await page.getByRole('button', { name: '保存待应用修改' }).click()
  await expect(page.getByText(/等待维护应用/)).toBeVisible()
  expect(writes).toBe(1)
  expect(
    await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage)),
  ).not.toContain('synthetic-admin')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({
    path: resolve(
      import.meta.dirname,
      `../../docs/validation/20261107-settings-${info.project.name}.png`,
    ),
    fullPage: true,
    scale: 'css',
  })
})
