import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { runPageSchema } from "../../src/api/client";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import {
  unnamedAxeViolations,
  type AxeBaselineEntry,
  type CoreSurface
} from "../support/axeBaseline";
import { completedRun, startedRun, waitingInputRun } from "../support/workflowV1";

const foundReference = "run1.Zm91bmQtcnVu";
const baseline = JSON.parse(
  readFileSync(resolve(import.meta.dirname, "../support/axeBaseline.json"), "utf8")
) as AxeBaselineEntry[];

const surfaces: readonly { surface: CoreSurface; path: string; ready: (page: Page) => Promise<void>; pseudoReady?: (page: Page) => Promise<void> }[] =
  [
    {
      surface: "studio",
      path: "/atelier",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
      }
    },
    {
      surface: "project",
      path: "/atelier/project",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
      },
      pseudoReady: async (page) => {
        await expect(page.getByText("[[[ Project ]]]", { exact: true })).toBeVisible();
      }
    },
    {
      surface: "new-run",
      path: "/atelier/new",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: "Choose a workflow" })).toBeVisible();
      }
    },
    {
      surface: "run",
      path: `/atelier/runs/${foundReference}`,
      ready: async (page) => {
        await expect(page.getByRole("navigation", { name: "Where you are" })).toBeVisible();
        await expect(page.getByRole("heading", { name: "Unnamed workflow" })).toBeVisible();
      }
    }
  ];

async function scanSurface(page: Page) {
  const scan = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag22aa"])
    .analyze();
  return scan.violations;
}

const studioViewports = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;

const projectViewports = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;

type ProjectRunReply = "common" | "empty" | "loading" | "retained-error";

function projectRuns() {
  return [
    startedRun({ run_id: "running project", public_run_reference: "run1.cnVubmluZyBwcm9qZWN0" }),
    waitingInputRun({ run_id: "waiting project", public_run_reference: "run1.d2FpdGluZyBwcm9qZWN0", latest_event_cursor: null }),
    completedRun({ run_id: "done project", public_run_reference: "run1.ZG9uZSBwcm9qZWN0" })
  ];
}

async function routeProjectReads(page: Page, read: () => ProjectRunReply, loading: { release: () => void; retainedReads: number }): Promise<void> {
  await page.route("**/atelier/api/v1/runs*", async (route: Route) => {
    const reply = read();
    if (reply === "loading") {
      await new Promise<void>((resolve) => { loading.release = resolve; });
      await route.fulfill({ json: { items: projectRuns(), next_after: null } });
      return;
    }
    if (reply === "retained-error" && loading.retainedReads++ > 0) {
      await route.abort();
      return;
    }
    await route.fulfill({ json: { items: reply === "empty" ? [] : projectRuns(), next_after: null } });
  });
  await page.route("**/atelier/api/v1/projects", (route) => route.fulfill({ json: { items: [{ public_project_reference: "project1.dGVzdA" }] } }));
  await page.route("**/atelier/api/v1/workflow-revisions*", (route) => route.fulfill({ json: { items: [], next_after_revision_hash: null } }));
  await page.route("**/atelier/api/v1/agent-configuration-revisions*", (route) => route.fulfill({ json: { items: [], next_after_agent_configuration_revision_hash: null } }));
}

async function expectStudioCopyFits(page: Page, desktop: boolean): Promise<void> {
  const heading = page.getByRole("heading", { name: "[[[ Board ]]]" });
  const board = page.locator(".studio-board");
  const home = page.locator(".studio-home");
  expect(await heading.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await board.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  if (desktop) {
    expect(await home.evaluate((element) => {
      const parent = element.parentElement!;
      const style = getComputedStyle(parent); return element.clientWidth === parent.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    })).toBe(true);
  }
}

test("proves(core-surfaces-have-no-unnamed-axe-violations): core surfaces have no unnamed axe-core violations", async ({ page }) => {
  for (const { surface, path, ready } of surfaces) {
    await page.goto(path);
    await ready(page);
    const unnamed = unnamedAxeViolations(surface, await scanSurface(page), baseline);
    expect(unnamed, `${surface}: ${JSON.stringify(unnamed, null, 2)}`).toEqual([]);
  }
});

test("core surfaces render owned display strings under a pseudo-locale", async ({ page }) => {
  for (const { path, ready, pseudoReady } of surfaces) {
    const separator = path.includes("?") ? "&" : "?";
    await page.goto(`${path}${separator}pseudo-locale=1`);
    if (path === "/atelier") {
      await expect(page.getByRole("heading", { name: "[[[ Board ]]]" })).toBeVisible();
    } else if (pseudoReady !== undefined) {
      await pseudoReady(page);
    } else {
      await ready(page);
    }
    const rail = page.getByRole("navigation", { name: "Workshop" });
    await expect(rail.getByText("[[[ Chat ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Board ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Workflows ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ History ]]]", { exact: true })).toBeVisible();
  }
});

test("proves(studio-entry-copy-is-owned-and-survives-pseudo-locale): Studio keeps header and confirmed empty copy visible at desktop and 390px", async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(window, "EventSource", { value: class extends EventTarget { constructor() { super(); queueMicrotask(() => this.dispatchEvent(new Event("open"))); } close() {} } }));
  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByText("[[[ Atelier ]]]", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Board ]]]" })).toBeVisible();
    await expect(page.getByRole("link", { name: "[[[ Start ]]]" })).toBeVisible();
    await expect(page.getByRole("article", { name: THE_ONE_PROJECT })).toBeVisible();
    await expectStudioCopyFits(page, viewport.width === 1280);
    await page.screenshot({ path: `test-results/studio-common-${viewport.width}.png`, fullPage: true });
  }

  await page.route("**/atelier/api/v1/runs*", (route) => route.fulfill({ json: { items: [], next_after: null } }));

  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));

    await expect(page.getByText("[[[ Atelier ]]]", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Board ]]]" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Nothing is running ]]]" })).toBeVisible();
    await expect(page.getByText("[[[ A workflow becomes a run, and a run is what this workshop shows. ]]]", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "[[[ Start a run ]]]" })).toBeVisible();
    await expectStudioCopyFits(page, viewport.width === 1280);
    await page.screenshot({ path: `test-results/studio-empty-${viewport.width}.png`, fullPage: true });
  }
});

test("Project keeps work, absence, loading, and retained failure readable", async ({ page }) => {
  expect(runPageSchema.safeParse({ items: projectRuns(), next_after: null }).success).toBe(true);
  let reply: ProjectRunReply = "common";
  const loading = { release: () => {}, retainedReads: 0 };
  await routeProjectReads(page, () => reply, loading);

  for (const pseudoLocale of [false, true]) {
    const locale = pseudoLocale ? "pseudo" : "normal";
    const suffix = pseudoLocale ? "?pseudo-locale=1" : "";
    for (const viewport of projectViewports) {
      await page.setViewportSize(viewport);
      reply = "common";
      await page.goto(`/atelier/project${suffix}`);
      await expect(page.getByText("Project runs unavailable")).toHaveCount(0);
      await expect(page.getByRole("region", { name: "Running" })).toContainText("▲Running");
      await expect(page.getByRole("region", { name: "Waiting for you" })).toContainText("⬢Waiting for you");
      await expect(page.getByRole("region", { name: "Done" })).toContainText("●Done");
      await page.screenshot({ path: `test-results/project-${locale}-${viewport.width}-common.png`, fullPage: true });

      reply = "empty";
      await page.goto(`/atelier/project${suffix}`);
      await expect(page.getByText(pseudoLocale ? "[[[ No runs here yet. ]]]" : "No runs here yet.")).toBeVisible();
      await page.screenshot({ path: `test-results/project-${locale}-${viewport.width}-empty.png`, fullPage: true });

      reply = "loading";
      await page.goto(`/atelier/project${suffix}`, { waitUntil: "domcontentloaded" });
      await expect(page.getByText("Looking…")).toBeVisible();
      await page.locator("main.workshop-stage").evaluate((stage) => { stage.scrollTop = 0; });
      await expect(page.getByRole("heading", { level: 1, name: THE_ONE_PROJECT })).toBeVisible();
      await expect(page.getByRole("link", { name: pseudoLocale ? "[[[ Start a run ]]]" : "Start a run" })).toBeVisible();
      await page.screenshot({ path: `test-results/project-${locale}-${viewport.width}-loading.png`, fullPage: true });
      loading.release();
      await expect(page.getByRole("region", { name: "Running" })).toBeVisible();

      reply = "retained-error";
      loading.retainedReads = 0;
      await page.goto(`/atelier/project${suffix}`);
      await expect(page.getByRole("region", { name: "Running" })).toBeVisible();
      await page.getByRole("button", { name: "Refresh project runs" }).click();
      await expect(page.getByText("Project runs unavailable")).toBeVisible();
      await expect(page.getByRole("region", { name: "Running" })).toBeVisible();
      await page.screenshot({ path: `test-results/project-${locale}-${viewport.width}-retained-error.png`, fullPage: true });
    }
  }
});
