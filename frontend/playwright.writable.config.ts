import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: [
    'writable-live.spec.ts',
    'writable-controls-live.spec.ts',
    'writable-readback-live.spec.ts',
  ],
  workers: 1,
  retries: 0,
  reporter: 'list',
  outputDir: '../.tmp/writable-live/browser-results',
  timeout: 720000,
  use: {
    baseURL: 'http://127.0.0.1:5175',
    trace: 'off',
    ...devices['Desktop Chrome'],
    channel: 'msedge',
  },
})
