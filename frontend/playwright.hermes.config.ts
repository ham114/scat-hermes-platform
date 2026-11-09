import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'hermes.spec.ts',
  outputDir: '../.tmp/h4/browser-results',
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:14173', trace: 'off' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], channel: 'msedge' } },
    { name: 'mobile', use: { ...devices['Pixel 5'], channel: 'msedge' } },
  ],
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 14173 --strictPort',
    url: 'http://127.0.0.1:14173',
    reuseExistingServer: false,
  },
})
