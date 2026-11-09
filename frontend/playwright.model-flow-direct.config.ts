import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: 'model-flow-direct-live.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 600000,
  outputDir: '../.tmp/model-flow/direct',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5175',
    channel: 'msedge',
    trace: 'off',
    actionTimeout: 30000,
    navigationTimeout: 60000,
  },
})
