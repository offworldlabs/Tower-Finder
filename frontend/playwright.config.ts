import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E test configuration.
 *
 * Environments (set via E2E_ENV):
 *   staging  → staging.retina.fm / staging-api.retina.fm / staging-testmap.retina.fm (default)
 *   prod     → retina.fm / api.retina.fm / testmap.retina.fm
 *   local    → localhost:5173 / localhost:8000
 */

const ENV = (process.env.E2E_ENV ?? "staging") as "staging" | "prod" | "local";

const HOSTS = {
  staging: {
    frontend:  "https://staging.retina.fm",
    api:       "https://staging-api.retina.fm",
    map:       "https://staging-map.retina.fm",
    testmap:   "https://staging-testmap.retina.fm",
    dash:      "https://staging-dash.retina.fm",
  },
  prod: {
    frontend:  "https://retina.fm",
    api:       "https://api.retina.fm",
    map:       "https://map.retina.fm",
    testmap:   "https://testmap.retina.fm",
    dash:      "https://dash.retina.fm",
  },
  local: {
    frontend:  "http://localhost:5173",
    api:       "http://localhost:8000",
    map:       "http://localhost:5173",
    testmap:   "http://localhost:5173",
    dash:      "http://localhost:5174",
  },
} as const;

export const hosts = HOSTS[ENV];

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: hosts.frontend,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
