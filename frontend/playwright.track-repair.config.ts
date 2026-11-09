import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'track-repair-live.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 240000,
  outputDir: '../.tmp/track-failure/browser',
  use: { baseURL: 'http://127.0.0.1:5175', trace: 'off', channel: 'msedge' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 5'] } },
  ],
})
