import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: 'query-completion-live.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 1200000,
  outputDir: '../.tmp/query-display-completion/live-browser',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5175',
    trace: 'off',
    channel: 'msedge',
  },
})
