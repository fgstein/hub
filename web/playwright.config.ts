import { defineConfig, devices } from "@playwright/test"

const PORT = Number(process.env.E2E_PORT ?? 8432)

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 800 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `rm -rf ../.trainhub/e2e && cd .. && uv run trainhub serve --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      TRAINHUB_EXECUTOR: "fake",
      TRAINHUB_INVENTORY: "web/e2e/inventory.e2e.yaml",
      TRAINHUB_DATA_DIR: ".trainhub/e2e",
      TRAINHUB_STATIC_DIR: "web/dist",
      TRAINHUB_LOG_LEVEL: "WARNING",
    },
  },
})
