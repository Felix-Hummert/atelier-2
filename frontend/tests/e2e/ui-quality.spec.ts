import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { runPageSchema } from "../../src/api/client";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { projectPageCopy } from "../../src/lib/projectPageCopy";
import { standingMarks, standingWords } from "../../src/lib/runState";
import { studioPageCopy } from "../../src/lib/studioPageCopy";
import {
  unnamedAxeViolations,
  type AxeBaselineEntry,
  type CoreSurface
} from "../support/axeBaseline";
import { completedRun, startedRun, waitingInputRun } from "../support/workflowV1";

const foundReference = "run1.Zm91bmQtcnVu";
/** The workflow the e2e fixture publishes, and the detail surface it opens. */
const seededWorkflowName = "iterate-code";

function wrapped(text: string): string {
  return `[[[ ${text} ]]]`;
}
const baseline = JSON.parse(
  readFileSync(resolve(import.meta.dirname, "../support/axeBaseline.json"), "utf8")
) as AxeBaselineEntry[];

/**
 * The fixture host publishes one unnamed format-1 document, so no workflow
 * detail exists on it to scan. The detail surface is therefore served from
 * routed reads — the page under test is the real one; only what the wire
 * answers is staged.
 */
async function stageNamedWorkflow(page: Page): Promise<void> {
  const revisionHash = "a".repeat(64);
  await page.route(/\/workflow-revisions\?/, (route) =>
    route.fulfill({ json: { items: [], next_after_revision_hash: null } })
  );
  const summary = {
    workflow_revision_hash: revisionHash,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name: seededWorkflowName,
    description: "build → review → fix, until green"
  };
  const graph = {
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    node_count: 3,
    agent_roles: ["builder", "reviewer"],
    orders: [],
    wait_answer_schemas: [],
    node_previews: [
      { id: "build", kind: "agent", role: "builder", instruction_start: null, depends_on: [] },
      { id: "review", kind: "agent", role: "reviewer", instruction_start: null, depends_on: ["build"] },
      { id: "open pr", kind: "action", role: null, instruction_start: null, depends_on: ["review"] }
    ],
    loops: [
      {
        id: "until_green",
        member_node_ids: ["build", "review"],
        maximum_rounds: 3,
        repeat_while: { node: "review", verdict: "revise" }
      }
    ],
    name: seededWorkflowName,
    description: "build → review → fix, until green"
  };
  await page.route("**/atelier/api/v1/workflow-revisions/by-name/*", (route) =>
    route.fulfill({
      json: {
        display_name: seededWorkflowName,
        lineage_id: "b".repeat(64),
        workflow_revision_hash: revisionHash,
        revision_number: 1
      }
    })
  );
  await page.route(`**/atelier/api/v1/workflow-revisions/${revisionHash}`, (route) =>
    route.fulfill({ json: { workflow_revision_hash: revisionHash, document_base64: "YQ==", graph } })
  );
  // A regular expression, not a glob: `workflow-revisions?*` also matches the
  // detail path, and a later route wins, so the list would swallow it.
  await page.route(/\/workflow-revisions\?/, (route) =>
    route.fulfill({ json: { items: [summary], next_after_revision_hash: null } })
  );
}

const surfaces: readonly {
  surface: CoreSurface;
  path: string;
  ready: (page: Page) => Promise<void>;
  pseudoReady?: (page: Page) => Promise<void>;
  prepare?: (page: Page) => Promise<void>;
}[] =
  [
    {
      surface: "chat",
      path: "/atelier/chat",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: "Chat", exact: true })).toBeVisible();
      },
      pseudoReady: async (page) => {
        await expect(page.getByRole("heading", { name: "[[[ Chat ]]]" })).toBeVisible();
      }
    },
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
    },
    {
      surface: "workflows",
      path: "/atelier/workflows",
      prepare: stageNamedWorkflow,
      ready: async (page) => {
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.getByRole("button", { name: seededWorkflowName })).toBeVisible();
      }
    },
    {
      surface: "workflow-detail",
      path: `/atelier/workflows/${encodeURIComponent(seededWorkflowName)}`,
      prepare: stageNamedWorkflow,
      ready: async (page) => {
        await expect(
          page.getByRole("heading", { level: 1, name: seededWorkflowName })
        ).toBeVisible();
      }
    },
    {
      surface: "history",
      path: "/atelier/history",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
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

/** One run in each standing a surface groups by. */
function runsOfEveryStanding() {
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
      await route.fulfill({ json: { items: runsOfEveryStanding(), next_after: null } });
      return;
    }
    if (reply === "retained-error" && loading.retainedReads++ > 0) {
      await route.abort();
      return;
    }
    await route.fulfill({ json: { items: reply === "empty" ? [] : runsOfEveryStanding(), next_after: null } });
  });
  await page.route("**/atelier/api/v1/projects", (route) => route.fulfill({ json: { items: [{ public_project_reference: "project1.dGVzdA" }] } }));
  await page.route("**/atelier/api/v1/workflow-revisions*", (route) => route.fulfill({ json: { items: [], next_after_revision_hash: null } }));
  await page.route("**/atelier/api/v1/agent-configuration-revisions*", (route) => route.fulfill({ json: { items: [], next_after_agent_configuration_revision_hash: null } }));
}

async function expectStudioCopyFits(page: Page, desktop: boolean): Promise<void> {
  const heading = page.getByRole("heading", { name: "[[[ Board ]]]" });
  const board = page.locator(".board-page");
  expect(await heading.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await board.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  if (desktop) {
    expect(await board.evaluate((element) => {
      const parent = element.parentElement!;
      const style = getComputedStyle(parent); return element.clientWidth === parent.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    })).toBe(true);
  }
}

// The skin answers `prefers-color-scheme`, so a contrast that only holds in
// light is only half a promise: both themes are scanned.
const themes = ["light", "dark"] as const;

test("proves(core-surfaces-have-no-unnamed-axe-violations): core surfaces have no unnamed axe-core violations", async ({ page }) => {
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const { surface, path, ready, prepare } of surfaces) {
      await prepare?.(page);
      await page.goto(path);
      await ready(page);
      const unnamed = unnamedAxeViolations(surface, await scanSurface(page), baseline);
      expect(unnamed, `${surface} in ${theme}: ${JSON.stringify(unnamed, null, 2)}`).toEqual([]);
    }
  }
});

test("core surfaces render owned display strings under a pseudo-locale", async ({ page }) => {
  for (const { path, ready, pseudoReady, prepare } of surfaces) {
    await prepare?.(page);
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
    await expect(rail.getByText("[[[ atelier ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Chat ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Board ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Workflows ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ History ]]]", { exact: true })).toBeVisible();
    // Only Settings and Profile are still marked later; every rail destination
    // opens a page, so none of them wears the marker any more.
    await expect(rail.getByText("[[[ (later) ]]]", { exact: true })).toHaveCount(1);
    await expect(rail.getByText("[[[ switch project ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Settings ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Profile ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText(THE_ONE_PROJECT, { exact: true })).toBeVisible();
  }
});

/**
 * The Board with work on it is staged, not inherited: the runs earlier specs
 * start finish on their own clock, so a Board read straight from the fixture
 * host is populated or empty depending on how long the spec before this one
 * took.
 */
async function stageBoardWithWork(page: Page): Promise<void> {
  await page.route("**/atelier/api/v1/runs*", (route) => {
    const state = new URL(route.request().url()).searchParams.get("state");
    const items = runsOfEveryStanding().filter((run) => run.state === state);
    return route.fulfill({ json: { items, next_after: null } });
  });
}

test("proves(studio-entry-copy-is-owned-and-survives-pseudo-locale): Studio keeps header and confirmed empty copy visible at desktop and 390px", async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(window, "EventSource", { value: class extends EventTarget { constructor() { super(); queueMicrotask(() => this.dispatchEvent(new Event("open"))); } close() {} } }));
  await stageBoardWithWork(page);
  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByText("[[[ Atelier ]]]", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Board ]]]" })).toBeVisible();
    // Starting a workflow lives in Workflows, not the Board head (#532): no
    // Start control of any kind sits beside the live indicator.
    await expect(page.getByRole("link", { name: /Start/ })).toHaveCount(0);
    await expectStudioCopyFits(page, viewport.width === 1280);
    await page.screenshot({ path: `test-results/studio-common-${viewport.width}.png`, fullPage: true });
  }

  await page.unroute("**/atelier/api/v1/runs*");
  await page.route("**/atelier/api/v1/runs*", (route) => route.fulfill({ json: { items: [], next_after: null } }));

  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));

    await expect(page.getByText("[[[ Atelier ]]]", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Board ]]]" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Nothing is running ]]]" })).toBeVisible();
    await expect(
      page.getByText(wrapped(studioPageCopy.emptyDescription), { exact: true })
    ).toBeVisible();
    await expect(page.getByRole("link", { name: wrapped(studioPageCopy.emptyStart) })).toBeVisible();
    await expectStudioCopyFits(page, viewport.width === 1280);
    await page.screenshot({ path: `test-results/studio-empty-${viewport.width}.png`, fullPage: true });
  }
});

test("Project keeps work, absence, loading, and retained failure readable", async ({ page }) => {
  expect(runPageSchema.safeParse({ items: runsOfEveryStanding(), next_after: null }).success).toBe(true);
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
      await expect(
        page.getByText(pseudoLocale ? wrapped(projectPageCopy.runsUnavailable) : projectPageCopy.runsUnavailable)
      ).toHaveCount(0);
      const work = page.getByRole("region", {
        name: pseudoLocale ? wrapped(projectPageCopy.workTitle) : projectPageCopy.workTitle
      });
      for (const standing of ["running", "waiting", "done"] as const) {
        const word = pseudoLocale ? wrapped(standingWords[standing]) : standingWords[standing];
        await expect(work).toContainText(`${standingMarks[standing]} 1 ${word}`);
      }
      await page.screenshot({ path: `test-results/project-${locale}-${viewport.width}-common.png`, fullPage: true });

      reply = "empty";
      await page.goto(`/atelier/project${suffix}`);
      await expect(
        page.getByText(pseudoLocale ? wrapped(projectPageCopy.noRuns) : projectPageCopy.noRuns)
      ).toBeVisible();
      await page.screenshot({ path: `test-results/project-${locale}-${viewport.width}-empty.png`, fullPage: true });

      reply = "loading";
      await page.goto(`/atelier/project${suffix}`, { waitUntil: "domcontentloaded" });
      await expect(page.getByText("Looking…")).toBeVisible();
      await page.locator("main.workshop-stage").evaluate((stage) => { stage.scrollTop = 0; });
      await expect(page.getByRole("heading", { level: 1, name: THE_ONE_PROJECT })).toBeVisible();
      await page.screenshot({ path: `test-results/project-${locale}-${viewport.width}-loading.png`, fullPage: true });
      loading.release();
      await expect(work).toBeVisible();

      // No manual refresh exists once a read is confirmed (#532): the only
      // reachable failure a fresh navigation can show is its own read
      // failing outright, recovered by the one accessible Retry.
      reply = "retained-error";
      loading.retainedReads = 1;
      await page.goto(`/atelier/project${suffix}`);
      await expect(
        page.getByText(pseudoLocale ? wrapped(projectPageCopy.runsUnavailable) : projectPageCopy.runsUnavailable)
      ).toBeVisible();
      const retry = page.getByRole("button", { name: "Retry project runs" });
      await expect(retry).toBeVisible();
      await page.screenshot({ path: `test-results/project-${locale}-${viewport.width}-unavailable.png`, fullPage: true });

      reply = "common";
      await retry.click();
      await expect(work).toContainText(
        pseudoLocale ? wrapped(standingWords.running) : standingWords.running
      );
      await expect(page.getByRole("button", { name: /project runs/ })).toHaveCount(0);
    }
  }
});
