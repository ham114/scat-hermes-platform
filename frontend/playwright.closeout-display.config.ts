import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: 'closeout-display-live.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 120000,
  outputDir: '../.tmp/platform-closeout/display',
  use: { baseURL: 'http://127.0.0.1:5175', channel: 'msedge', trace: 'off' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 5'] } },
  ],
})
