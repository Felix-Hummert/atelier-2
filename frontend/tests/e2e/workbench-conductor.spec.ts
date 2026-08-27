import { expect, test, type Page } from "@playwright/test";

import { conductorChatCopy } from "../../src/lib/conductorChatCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";

/**
 * The chat wire, driven as the operator drives it (#7): one served instance
 * first shows the honest "no conductor" refusal, then — after the harness
 * publishes the production conductor catalog and the fake doors-shaped
 * executor answers — a typed message becomes ONE engine run whose report
 * comes back into the same conversation.
 *
 * The reply text is `CONDUCTOR_FAKE_ANSWER` in `tests/e2e/serve_cockpit.py`,
 * asserted verbatim so the words a human reads are the proof.
 */
const CONDUCTOR_FAKE_ANSWER =
  "Nothing started: the workbench probe only asked for an answer.";

const widths = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;

async function photograph(page: Page, name: string): Promise<void> {
  for (const viewport of widths) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.screenshot({
      path: test.info().outputPath(`${name}-${viewport.name}.png`),
      fullPage: true
    });
  }
  await page.setViewportSize({ width: 1280, height: 900 });
}

test("a message meets the honest refusal without a conductor, and becomes one episode with one", async ({ page }) => {
  test.setTimeout(120_000);

  // This suite shares one server across every spec file (#742): a conductor
  // another file already seeded would still answer here. This test's own
  // first act resets the server to its cold-boot baseline -- guaranteed
  // unseeded -- instead of depending on running before `workbench-conductor`
  // in the file listing.
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });

  // Before any conductor exists: the composer says so, and a sent message
  // gets the standing honest answer -- nothing pretends to listen.
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await expect(page.getByText(workbenchPageCopy.composerHint)).toBeVisible();
  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Hallo, hört mir jemand zu?");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  await expect(page.getByText(workbenchPageCopy.conductorAbsent)).toBeVisible();
  await photograph(page, "workbench-not-connected");

  // The harness publishes the production conductor catalog: schemas, the
  // conductor document from its own owner and agent configuration.
  const seeded = await page.request.post("/__e2e/seed-conductor");
  expect(seeded.ok()).toBeTruthy();

  // A reload resolves the connection fresh; the composer now says a
  // conductor is connected, and the same surface carries a real episode.
  await page.reload();
  await expect(page.getByText(conductorChatCopy.composerHint)).toBeVisible();
  await page
    .getByLabel(workbenchPageCopy.composerLabel)
    .fill("Starte nichts, antworte nur kurz.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();

  // The reply is the report of one real engine run of the published conductor
  // document, returned through the run's own event stream.
  await expect(page.getByText(CONDUCTOR_FAKE_ANSWER)).toBeVisible({ timeout: 60_000 });
  const episodeLink = page.getByRole("link", { name: conductorChatCopy.openEpisode });
  await expect(episodeLink).toBeVisible();
  await photograph(page, "workbench-conductor-reply");

  // The linked run page is the reply's manual counterpart: the episode is an
  // ordinary run anyone can open (Keine-Sonderautoritaet, #7).
  await episodeLink.click();
  await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
});
