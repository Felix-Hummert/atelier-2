import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";

const frontendRoot = import.meta.dirname;
const repositoryRoot = resolve(frontendRoot, "..");
const resultRoot = resolve(frontendRoot, "test-results");
const runtimeRoot = resolve(frontendRoot, ".playwright-runtime");
const port = 8423;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["json", { outputFile: "../reports/frontend.playwright.json" }]],
  outputDir: resultRoot,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: "chromium",
    channel: "chromium",
    headless: true,
    trace: "retain-on-failure"
  },
  webServer: {
    command: "uv run --locked python tests/e2e/serve_cockpit.py",
    cwd: repositoryRoot,
    env: {
      ATELIER2_E2E_ROOT: runtimeRoot,
      ATELIER2_E2E_PORT: String(port),
      ATELIER2_E2E_FRONTEND_DIST: resolve(frontendRoot, "dist")
    },
    gracefulShutdown: { signal: "SIGINT", timeout: 30_000 },
    url: `http://127.0.0.1:${port}/atelier/api/v1/health`,
    reuseExistingServer: false,
    timeout: 30_000
  }
});
