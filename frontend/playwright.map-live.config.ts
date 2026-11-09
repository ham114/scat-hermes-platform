import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'trajectory-map-live.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 90000,
  outputDir: '../.tmp/map-view-repair/browser',
  use: { baseURL: 'http://127.0.0.1:5175', trace: 'off', channel: 'msedge' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 5'] } },
  ],
})
