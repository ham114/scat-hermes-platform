import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e',
  testMatch: [
    'workspace.spec.ts',
    'history.spec.ts',
    'skills.spec.ts',
    'memory-settings.spec.ts',
    'trajectory-workspace.spec.ts',
    'hermes.spec.ts',
    'model-tasks.spec.ts',
    'h6-display.spec.ts',
    'workflows.spec.ts',
  ],
  outputDir: '../.tmp/memory-settings-ui/browser-results',
  reporter: 'list',
  workers: 2,
  use: { baseURL: 'http://127.0.0.1:4173', trace: 'off' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], channel: 'msedge' } },
    { name: 'mobile', use: { ...devices['Pixel 5'], channel: 'msedge' } },
  ],
  webServer: {
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
  },
})
