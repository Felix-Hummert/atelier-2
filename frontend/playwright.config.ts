import { defineConfig } from "@playwright/test";
import { readdirSync } from "node:fs";
import { resolve } from "node:path";

import { chooseHarnessPort } from "./tests/e2e/harness-port";

const frontendRoot = import.meta.dirname;
const repositoryRoot = resolve(frontendRoot, "..");
const resultRoot = resolve(frontendRoot, "test-results");
const runtimeRoot = resolve(frontendRoot, ".playwright-runtime");
const e2eDirectory = "tests/e2e";
const port = await chooseHarnessPort();

/**
 * The suite's own order owner (#742): the file listing below, read fresh from
 * the filesystem on every config load -- never a second, hardcoded copy of it
 * anywhere else (CI YAML, `package.json`). Playwright always schedules a
 * `workers: 1` run's top-level suites in project-declaration order, not file
 * discovery order, so a project per file (below) is what actually lets
 * `E2E_ORDER=reversed` run the identical set of specs against the same shared
 * server in the opposite order -- proving the isolation #742 built holds,
 * rather than merely asserting it.
 */
const specFileNames = readdirSync(resolve(frontendRoot, e2eDirectory))
  .filter((name) => name.endsWith(".spec.ts"))
  .sort();
const orderedSpecFileNames =
  process.env.E2E_ORDER === "reversed" ? [...specFileNames].reverse() : specFileNames;

export default defineConfig({
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
    command: "npm --prefix frontend run build && uv run --locked python tests/e2e/serve_cockpit.py",
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
  },
  projects: orderedSpecFileNames.map((fileName) => ({
    name: fileName,
    testDir: e2eDirectory,
    testMatch: fileName
  }))
});
