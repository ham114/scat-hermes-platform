import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: 'query-display-rounds-live.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 1200000,
  outputDir: '../.tmp/query-display-rounds/browser',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5175',
    trace: 'off',
    channel: 'msedge',
  },
})
