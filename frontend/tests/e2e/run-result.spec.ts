import { expect, test, type Page } from "@playwright/test";

import { conductorChatCopy } from "../../src/lib/conductorChatCopy";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { runResultCopy } from "../../src/lib/runResultCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";

/**
 * #716: a finished run's page shows its own result without a click, and the
 * node panel's Result tab renders the identical readable form with the raw
 * JSON behind a collapsed "Raw" disclosure.
 *
 * The scenario is the exact one the issue was filed against: the harness's
 * fake conductor episode (`/__e2e/seed-conductor` in `tests/e2e/serve_cockpit.py`)
 * answers with `CONDUCTOR_REPORT_SCHEMA`'s own shape --
 * `{"answer": "...", "started_run_ids": []}` -- the same declared object the
 * bug report's screenshot showed printed as a raw JSON line.
 */
const CONDUCTOR_FAKE_ANSWER =
  "Nothing started: the workbench probe only asked for an answer.";
// The exact bytes `json.dumps` in `tests/e2e/serve_cockpit.py` wrote -- its
// default separators, not a compact re-serialization this page invents.
const CONDUCTOR_FAKE_REPORT_RAW = `{"answer": "${CONDUCTOR_FAKE_ANSWER}", "started_run_ids": []}`;

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;
const themes = ["light", "dark"] as const;

async function shoot(page: Page, name: string): Promise<void> {
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.waitForTimeout(150);
      await page.screenshot({
        path: test.info().outputPath(`${name}-${theme}-${viewport.name}.png`),
        fullPage: true
      });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

async function completedConductorRun(page: Page): Promise<string> {
  expect((await page.request.post("/__e2e/seed-conductor")).ok()).toBeTruthy();
  await page.goto("/atelier/chat");
  await page.reload();
  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Starte nichts, antworte nur kurz.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  const episodeLink = page.getByRole("link", { name: conductorChatCopy.openEpisode });
  await expect(episodeLink).toBeVisible({ timeout: 60_000 });
  await episodeLink.click();
  await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
  return page.url();
}

test("a finished run's own result reads above the graph, unclicked, never as a raw JSON line", async ({ page }) => {
  test.setTimeout(120_000);

  await completedConductorRun(page);
  await expect(page.getByLabel("Where this run stands")).toContainText("Done");

  // The outcome is on the page before any node is opened.
  const outcome = page.getByRole("region", { name: runPageCopy.tabResult });
  await expect(outcome).toBeVisible();
  await expect(outcome.getByText(CONDUCTOR_FAKE_ANSWER, { exact: true })).toBeVisible();
  // Never a raw JSON line open on the main surface: the exact bytes stay
  // behind a disclosure nobody opened.
  await expect(outcome.getByText(CONDUCTOR_FAKE_REPORT_RAW)).not.toBeVisible();
  await shoot(page, "run-outcome-unclicked");

  // The node panel's Result tab renders the identical readable sentence, the
  // exact JSON kept behind a disclosure the operator has not opened.
  await page.getByRole("button", { name: "conduct — Done" }).click();
  const panel = page.getByRole("complementary");
  await expect(panel.getByText(CONDUCTOR_FAKE_ANSWER, { exact: true })).toBeVisible();
  const rawToggle = panel.getByText(runResultCopy.raw, { exact: true });
  await expect(rawToggle).toBeVisible();
  await expect(panel.getByText(CONDUCTOR_FAKE_REPORT_RAW)).not.toBeVisible();
  await shoot(page, "run-node-result-collapsed");

  await rawToggle.click();
  await expect(panel.getByText(CONDUCTOR_FAKE_REPORT_RAW)).toBeVisible();
  await shoot(page, "run-node-result-raw-open");
});
