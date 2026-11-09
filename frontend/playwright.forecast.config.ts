import { defineConfig } from '@playwright/test'
import baseline from './playwright.workspace.config'

export default defineConfig({
  ...baseline,
  testMatch: [
    ...(Array.isArray(baseline.testMatch) ? baseline.testMatch : []),
    'forecast-contract.spec.ts',
  ],
  outputDir: '../.tmp/forecast-contract/browser',
  retries: 0,
})
