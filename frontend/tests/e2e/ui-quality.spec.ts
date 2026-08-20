import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  unnamedAxeViolations,
  type AxeBaselineEntry,
  type CoreSurface
} from "../../src/lib/axeBaseline";

const foundReference = "run1.Zm91bmQtcnVu";
const baseline = JSON.parse(
  readFileSync(resolve(import.meta.dirname, "../../src/lib/axeBaseline.json"), "utf8")
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
    await ready(page);
    const rail = page.getByRole("navigation", { name: "Workshop" });
    await expect(rail.getByText("[[[ Studio ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Projekte ]]]", { exact: true })).toBeVisible();
    await expect(rail.getByText("[[[ Runs ]]]", { exact: true })).toBeVisible();
  }
});
