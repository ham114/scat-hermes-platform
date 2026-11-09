import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: 'readonly-live.spec.ts',
  outputDir: '../.tmp/frontend-workspace/readonly-results',
  reporter: 'list',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:5174', trace: 'off' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], channel: 'msedge' } },
    { name: 'mobile', use: { ...devices['Pixel 5'], channel: 'msedge' } },
  ],
})
