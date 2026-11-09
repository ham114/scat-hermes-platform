import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: 'model-flow-live.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 720000,
  outputDir: '../.tmp/model-flow/live',
  use: {
    actionTimeout: 30000,
    navigationTimeout: 60000,
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5175',
    channel: 'msedge',
    trace: 'off',
  },
})
