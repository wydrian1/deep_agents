import { defineConfig } from '@playwright/test';

// The tests drive a RUNNING service (test-mode agents only, no gateway needed):
//   DATABASE_URL=... ALLOW_TEST_MODE=true uvicorn deep_agents_app.main:app --port 8000
//   E2E_BASE_URL=http://127.0.0.1:8000 npm run e2e
export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:8000',
    locale: 'en-US',
    trace: 'retain-on-failure',
  },
});
