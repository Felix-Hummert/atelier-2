import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  unnamedAxeViolations,
  type AxeBaselineEntry,
  type CoreSurface
} from "../support/axeBaseline";

const foundReference = "run1.Zm91bmQtcnVu";
const baseline = JSON.parse(
  readFileSync(resolve(import.meta.dirname, "../support/axeBaseline.json"), "utf8")
) as AxeBaselineEntry[];

const surfaces: readonly { surface: CoreSurface; path: string; ready: (page: Page) => Promise<void> }[] =
  [
    {
      surface: "studio",
      path: "/atelier",
      ready: async (page) => {
        await expect(page.getByRole("heading", { name: "Studio" })).toBeVisible();
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
        await expect(page.getByRole("heading", { name: /Run / })).toBeVisible();
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

async function expectStudioCopyFits(page: Page, desktop: boolean): Promise<void> {
  const heading = page.getByRole("heading", { name: "[[[ Studio ]]]" });
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
  for (const { path, ready } of surfaces) {
    const separator = path.includes("?") ? "&" : "?";
    await page.goto(`${path}${separator}pseudo-locale=1`);
    if (path === "/atelier") {
      await expect(page.getByRole("heading", { name: "[[[ Studio ]]]" })).toBeVisible();
    } else {
      await ready(page);
    }
    const rail = page.getByRole("navigation", { name: "Workshop" });
    await expect(rail.getByText("[[[ Studio ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Projekte ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Runs ]]]", { exact: true })).toBeVisible();
  }
});

test("proves(studio-entry-copy-is-owned-and-survives-pseudo-locale): Studio keeps header and confirmed empty copy visible at desktop and 390px", async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(window, "EventSource", { value: class extends EventTarget { constructor() { super(); queueMicrotask(() => this.dispatchEvent(new Event("open"))); } close() {} } }));
  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByText("[[[ Atelier ]]]", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Studio ]]]" })).toBeVisible();
    await expect(page.getByRole("link", { name: "[[[ Start ]]]" })).toBeVisible();
    await expect(page.getByRole("article", { name: "This workshop" })).toBeVisible();
    await expectStudioCopyFits(page, viewport.width === 1280);
    await page.screenshot({ path: `test-results/studio-common-${viewport.width}.png`, fullPage: true });
  }

  await page.route("**/atelier/api/v1/runs*", (route) => route.fulfill({ json: { items: [], next_after: null } }));

  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));

    await expect(page.getByText("[[[ Atelier ]]]", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Studio ]]]" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "[[[ Nothing is running ]]]" })).toBeVisible();
    await expect(page.getByText("[[[ A workflow becomes a run, and a run is what this workshop shows. ]]]", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "[[[ Start a run ]]]" })).toBeVisible();
    await expectStudioCopyFits(page, viewport.width === 1280);
    await page.screenshot({ path: `test-results/studio-empty-${viewport.width}.png`, fullPage: true });
  }
});
