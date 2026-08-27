import { expect, test, type Page } from "@playwright/test";
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { conductorChatCopy } from "../../src/lib/conductorChatCopy";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { runResultCopy } from "../../src/lib/runResultCopy";
import { standingWords } from "../../src/lib/runState";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";

/**
 * #666 Result tab against the ruling that the run head is the one standing
 * sentence and the node's Result tab carries the decoded declared output
 * (blessed frame `#v8-14-run-log` in `docs/requirements/0003-ziel-ui-mockup-v8.html`).
 *
 * Result shots of this journey, same viewports and themes as `run-log.spec.ts`,
 * of the same room (workshop rail + stage):
 *   test-results/result-666/head-{1280,390}-{light,dark}.png
 *   test-results/result-666/result-{1280,390}-{light,dark}.png
 *
 * The scenario is the exact one the issue was filed against: the harness's
 * fake conductor episode (`/__e2e/seed-conductor` in `tests/e2e/serve_cockpit.py`)
 * answers with `CONDUCTOR_REPORT_SCHEMA`'s own shape --
 * `{"answer": "...", "started_run_ids": []}` -- the same declared object the
 * bug report's screenshot showed printed as a raw JSON line. Its one node is
 * also the run's sink, so this journey is the duplicate-answer case; the
 * node panel's ordinary rendering of a non-sink node's own answer is proven
 * at the component level in `tests/app/readableResultDisplay.test.ts`.
 *
 * `/__e2e/seed-conductor` durably mutates the one shared harness server's
 * state for the rest of the run, so this used to need a `zz-` name sorting it
 * after `workbench-conductor.spec.ts`, whose own first act asserted the
 * *pre*-seed "no conductor" state (#742). That file now resets the server to
 * its own cold-boot baseline itself instead of depending on file order, so
 * this spec no longer needs to sort after it -- it seeds and confirms its
 * own conductor connection either way (below), regardless of what any other
 * spec already did to the shared server.
 */
const CONDUCTOR_FAKE_ANSWER =
  "Nothing started: the workbench probe only asked for an answer.";
// The exact bytes `json.dumps` in `tests/e2e/serve_cockpit.py` wrote -- its
// default separators, not a compact re-serialization this page invents.
const CONDUCTOR_FAKE_REPORT_RAW = `{"answer": "${CONDUCTOR_FAKE_ANSWER}", "started_run_ids": []}`;

const frontendRoot = resolve(import.meta.dirname, "../..");
const shotDir = resolve(frontendRoot, "test-results/result-666");

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;
const themes = ["light", "dark"] as const;

async function shoot(page: Page, name: string): Promise<void> {
  mkdirSync(shotDir, { recursive: true });
  const frame = page.locator(".workshop");
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.waitForTimeout(200);
      const artifact = test.info().outputPath(`${name}-${theme}-${viewport.name}.png`);
      await frame.screenshot({ path: artifact });
      copyFileSync(artifact, `${shotDir}/${name}-${viewport.name}-${theme}.png`);
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

async function completedConductorRun(page: Page): Promise<string> {
  // This suite shares one server across every spec file and across
  // `--repeat-each` (#742): a previous seed of the same conductor catalog
  // would conflict on the model registry. Reset to the cold-boot baseline,
  // then seed, so this journey owns its own connected conductor.
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
  expect((await page.request.post("/__e2e/seed-conductor")).ok()).toBeTruthy();
  await page.goto("/atelier/chat");
  // The precondition this journey needs is a *connected* conductor, not
  // merely a successful seed call: this suite's server is shared across
  // spec files that run in no particular order (#742), so it proves the
  // connection itself rather than assuming the seed above was the only
  // thing that could have changed it.
  await expect(page.getByText(conductorChatCopy.composerHint)).toBeVisible({ timeout: 15_000 });
  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Starte nichts, antworte nur kurz.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  const episodeLink = page.getByRole("link", { name: conductorChatCopy.openEpisode });
  await expect(episodeLink).toBeVisible({ timeout: 60_000 });
  await episodeLink.click();
  await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
  return page.url();
}

test("the Result tab carries the decoded result; the run head is only the standing sentence", async ({
  page
}) => {
  test.setTimeout(120_000);

  await completedConductorRun(page);
  await expect(page.getByLabel("Where this run stands")).toContainText(standingWords.done, {
    timeout: 30_000
  });

  await expect(page.locator("#run-outcome")).toHaveCount(0);
  await expect(page.getByRole("region", { name: runPageCopy.tabResult })).toHaveCount(0);
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER, { exact: true })).toHaveCount(0);
  await shoot(page, "head");

  await page.getByRole("button", { name: "conduct — Done" }).click();
  const panel = page.getByRole("complementary");
  await expect(panel.getByRole("heading", { name: "conduct" })).toBeVisible();
  await expect(panel.getByRole("tab", { name: runPageCopy.tabResult })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  await expect(panel.getByText(CONDUCTOR_FAKE_ANSWER, { exact: true })).toBeVisible();
  const exactFold = panel.locator("details").filter({ hasText: runResultCopy.exactText });
  await expect(exactFold).toBeVisible();
  await expect(exactFold).not.toHaveAttribute("open");
  await expect(panel.getByText(CONDUCTOR_FAKE_REPORT_RAW)).not.toBeVisible();
  await expect(panel.getByRole("link", { name: "Shown above" })).toHaveCount(0);
  await expect(page.locator("#run-outcome")).toHaveCount(0);
  await shoot(page, "result");

  for (const theme of themes) {
    for (const viewport of widths) {
      for (const name of ["head", "result"] as const) {
        const stable = `${shotDir}/${name}-${viewport.name}-${theme}.png`;
        expect(existsSync(stable), `missing result shot ${stable}`).toBe(true);
      }
    }
  }
});
