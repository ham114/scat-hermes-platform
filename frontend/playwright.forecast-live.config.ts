import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: 'forecast-live.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 720000,
  outputDir: '../.tmp/forecast-contract/live-browser',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5175',
    channel: 'msedge',
    trace: 'off',
  },
})
