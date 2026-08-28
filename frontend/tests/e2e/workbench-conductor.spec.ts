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
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;

const themes = ["light", "dark"] as const;

type ReconciliationFixtureRun = {
  public_run_reference: string;
  workflow_revision_hash: string;
  waiting: {
    type: "WAITING_RECONCILIATION";
    intent_state_version: number;
  };
};

type InputFixtureRun = {
  public_run_reference: string;
  workflow_revision_hash: string;
  waiting: {
    type: "WAITING_INPUT";
    node_id: string;
  };
};

/** Retire the harness's two cold-boot reconciliation examples. This scenario
 * proves exactly six open decisions, rather than accidentally borrowing those
 * unrelated fixture waits into the Workbench count or shelf. */
async function retireReconciliationFixtures(page: Page): Promise<void> {
  const listed = await page.request.get("/atelier/api/v1/runs?state=WAITING_RECONCILIATION&limit=50");
  expect(listed.status()).toBe(200);
  const { items } = (await listed.json()) as {
    items: ReconciliationFixtureRun[];
  };
  expect(items).toHaveLength(2);

  for (const run of items) {
    expect(run.waiting.type).toBe("WAITING_RECONCILIATION");
    const retired = await page.request.post(`/atelier/api/v1/runs/${run.public_run_reference}/reconciliations`, {
      headers: { "content-type": "application/json" },
      data: {
        command_id: `reconcile-e2e-isolation-${run.public_run_reference}`,
        expected_intent_state_version: run.waiting.intent_state_version,
        actor: "Playwright fixture isolation",
        evidence: "This six-decision scenario retires the cold-boot examples.",
        determination: { type: "operator_authoritative_absence" }
      }
    });
    expect([200, 202]).toContain(retired.status());
  }

  await expect(async () => {
    const remaining = await page.request.get("/atelier/api/v1/runs?state=WAITING_RECONCILIATION&limit=50");
    expect(remaining.status()).toBe(200);
    expect(((await remaining.json()) as { items: unknown[] }).items).toHaveLength(0);
  }).toPass({ timeout: 20_000 });

  // Reconciliation advances the fixtures asynchronously. Follow each captured
  // baseline run through its V1 input wait to completion, rather than merely
  // sampling the global waiting list before its worker has reached that wait.
  await expect(async () => {
    const states: string[] = [];
    for (const fixture of items) {
      const current = await page.request.get(`/atelier/api/v1/runs/${fixture.public_run_reference}`);
      expect(current.status()).toBe(200);
      const run = (await current.json()) as InputFixtureRun & { state: string };
      states.push(run.state);
      if (run.state === "WAITING_INPUT") {
        expect(run.waiting.type).toBe("WAITING_INPUT");
        const fence = await page.request.get(
          `/__e2e/current-wait-execution?public_run_reference=${encodeURIComponent(run.public_run_reference)}`
        );
        expect(fence.status()).toBe(200);
        const { expected_node_execution_id: expectedNodeExecutionId } =
          (await fence.json()) as { expected_node_execution_id: string };
        const answered = await page.request.post(`/atelier/api/v1/runs/${run.public_run_reference}/answers`, {
          headers: { "content-type": "application/json" },
          data: {
            workflow_revision_hash: run.workflow_revision_hash,
            node_id: run.waiting.node_id,
            expected_node_execution_id: expectedNodeExecutionId,
            actor: "operator",
            answer_base64: "MQ=="
          }
        });
        expect(answered.status()).toBe(202);
      }
    }
    expect(states).toEqual(["COMPLETED", "COMPLETED"]);
  }).toPass({ timeout: 20_000 });
}

async function photograph(page: Page, name: string, scrollMobileMainToEnd = false): Promise<void> {
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({
        width: viewport.width,
        height: viewport.height
      });
      if (scrollMobileMainToEnd && viewport.width === 390) {
        await placeConversationAboveComposer(page);
      }
      await page.screenshot({
        path: test.info().outputPath(`${name}-${theme}-${viewport.name}.png`),
        fullPage: true
      });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

async function placeConversationAboveComposer(page: Page): Promise<void> {
  await page.getByRole("main").evaluate((element) => {
    element.scrollTop = element.scrollHeight - element.clientHeight;
  });
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

test("keeps many open decisions bounded, with one hairline and one promoted stage", async ({ page }) => {
  test.setTimeout(120_000);

  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
  await retireReconciliationFixtures(page);

  const schema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"type":"boolean"}'
  });
  expect([200, 201]).toContain(schema.status());
  const schemaRevisionHash = (await schema.json()).schema_revision_hash as string;

  for (let index = 1; index <= 6; index += 1) {
    const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", {
      headers: { "content-type": "application/yaml" },
      data: [
        "format_version: 3",
        `name: Decision ${index}`,
        "nodes:",
        "  - id: ask",
        "    type: wait",
        `    prompt: Should decision ${index} move on?`,
        `    outputs: [{name: answer, schema: {ref: decision, revision: ${schemaRevisionHash}}}]`,
        ""
      ].join("\n")
    });
    expect(workflow.status()).toBe(201);
    const started = await page.request.post("/atelier/api/v1/runs", {
      data: {
        workflow_format_version: 3,
        run_id: `workbench/bounded-decision-${index}`,
        workflow_revision_hash: (await workflow.json()).workflow_revision_hash as string,
        agent_bindings: [],
        orders: []
      }
    });
    expect(started.status()).toBe(201);
  }

  await page.goto("/atelier/chat");
  await expect(page.getByRole("link", { name: "Workbench 6 needs you" })).toBeVisible({ timeout: 20_000 });
  const pinnedRegion = page.getByRole("region", {
    name: workbenchPageCopy.pinnedDecisionsLabel
  });
  const decisions = pinnedRegion.getByRole("region");
  await expect(decisions).toHaveCount(6, { timeout: 20_000 });
  const expandedDecision = pinnedRegion.locator(".pinned-decision:not(.pinned-decision-compact)");
  await expect(expandedDecision).toHaveCount(1);
  const expandedRunDoor = expandedDecision.getByRole("link", {
    name: workbenchPageCopy.openTheRun
  });
  await expect(expandedRunDoor).toBeVisible();
  const compactControls = pinnedRegion.getByRole("button", {
    name: workbenchPageCopy.answerDecision
  });
  await expect(compactControls).toHaveCount(5);

  // Each compact decision can be brought fully into the rail by its own
  // scrolling surface; the already-visible expanded decision owns its run
  // door. This keeps every decision reachable without choosing a row count.
  const everyCompactControlRevealsInRail = await compactControls.evaluateAll((controls) => {
    const rail = controls[0]?.closest<HTMLElement>(".needs-you");
    if (rail === null || rail === undefined) return false;
    return controls.every((control) => {
      control.scrollIntoView({ block: "nearest" });
      const railBox = rail.getBoundingClientRect();
      const controlBox = control.getBoundingClientRect();
      return controlBox.top >= railBox.top && controlBox.bottom <= railBox.bottom;
    });
  });
  expect(everyCompactControlRevealsInRail).toBe(true);
  await expect.poll(() => pinnedRegion.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);

  await pinnedRegion.evaluate((element) => {
    element.scrollTop = 0;
  });
  const promotedDecisionLabel = await compactControls.first().locator("..").getAttribute("aria-labelledby");
  if (promotedDecisionLabel === null) throw new Error("The compact decision has no accessible label.");
  const promotedDecision = pinnedRegion.locator(`section[aria-labelledby="${promotedDecisionLabel}"]`);
  await compactControls.first().click();
  await expect(expandedDecision).toHaveCount(1);
  await expect(promotedDecision).not.toHaveClass(/pinned-decision-compact/);
  await expect(promotedDecision.getByRole("link", { name: workbenchPageCopy.openTheRun })).toHaveCount(1);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("main").evaluate((element) => {
    element.scrollTo(0, 0);
  });
  await pinnedRegion.evaluate((element) => {
    element.scrollTop = 0;
  });
  await expect(compactControls).toHaveCount(5);
  const compactRail = await pinnedRegion.evaluate((element) => {
    const style = getComputedStyle(element);
    const rail = element.getBoundingClientRect();
    const controls = Array.from(element.querySelectorAll<HTMLElement>(".compact-answer"));
    const controlFits = (control: HTMLElement) => {
      const box = control.getBoundingClientRect();
      return box.top >= rail.top && box.bottom <= rail.bottom;
    };
    const controlFallsBeyondTheFold = (control: HTMLElement) => {
      const box = control.getBoundingClientRect();
      return box.top >= rail.bottom || box.bottom > rail.bottom;
    };
    return {
      fullyVisibleControlCount: controls.filter(controlFits).length,
      beyondFoldControlCount: controls.filter(controlFallsBeyondTheFold).length,
      canScroll: element.scrollHeight > element.clientHeight,
      maskImage: style.maskImage,
      maxHeight: style.maxHeight,
      overflowY: style.overflowY
    };
  });
  // The picture promises a bounded rail with a direct compact move, while the
  // remaining controls stay behind its scrolling, faded fold. Which row first
  // crosses that fold varies with the browser's font metrics.
  expect(compactRail.maxHeight).not.toBe("none");
  expect(compactRail.overflowY).toBe("auto");
  expect(compactRail.maskImage).not.toBe("none");
  expect(compactRail.fullyVisibleControlCount).toBeGreaterThanOrEqual(1);
  expect(compactRail.beyondFoldControlCount).toBeGreaterThanOrEqual(1);
  expect(compactRail.canScroll).toBe(true);

  await page.getByLabel(workbenchPageCopy.composerLabel).fill("Keep this conversation on screen.");
  await page.getByRole("button", { name: workbenchPageCopy.send }).click();
  await placeConversationAboveComposer(page);
  const pinnedBox = await pinnedRegion.boundingBox();
  const composerBox = await page.getByRole("form", { name: workbenchPageCopy.composerRegionLabel }).boundingBox();
  const conversationBox = await page.getByRole("list", { name: workbenchPageCopy.transcriptLabel }).boundingBox();
  expect(pinnedBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  expect(conversationBox).not.toBeNull();
  if (pinnedBox === null || composerBox === null || conversationBox === null) {
    throw new Error("The bounded Workbench did not lay out every fixture.");
  }
  expect(pinnedBox.y).toBeGreaterThanOrEqual(0);
  expect(pinnedBox.y + pinnedBox.height).toBeLessThanOrEqual(844);
  expect(composerBox.y).toBeGreaterThanOrEqual(0);
  expect(composerBox.y + composerBox.height).toBeLessThanOrEqual(844);
  expect(conversationBox.y).toBeGreaterThanOrEqual(0);
  expect(conversationBox.y + conversationBox.height).toBeLessThanOrEqual(844);
  expect(conversationBox.y).toBeGreaterThanOrEqual(pinnedBox.y + pinnedBox.height);
  // Browser layout may divide the one-pixel composer hairline fractionally;
  // the conversation still clears its painted content.
  expect(conversationBox.y + conversationBox.height).toBeLessThanOrEqual(composerBox.y + 1);

  const borderTokens = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    return {
      compact: style.getPropertyValue("--edge").trim(),
      expanded: style.getPropertyValue("--edge-strong").trim()
    };
  });
  const expandedBorderWidth = await expandedDecision.evaluate((element) => getComputedStyle(element).borderTopWidth);
  const compactBorderWidth = await decisions
    .filter({
      has: page.getByRole("button", { name: workbenchPageCopy.answerDecision })
    })
    .first()
    .evaluate((element) => getComputedStyle(element).borderTopWidth);
  expect(expandedBorderWidth).toBe(borderTokens.expanded);
  expect(compactBorderWidth).toBe(borderTokens.compact);

  const compactLayout = await compactControls.first().evaluate((element) => {
    const question = element.querySelector<HTMLElement>(".compact-question");
    if (question === null) throw new Error("The compact decision question is missing.");
    const controlStyle = getComputedStyle(element);
    const questionStyle = getComputedStyle(question);
    return {
      flexWrap: controlStyle.flexWrap,
      minWidth: questionStyle.minWidth,
      overflow: questionStyle.overflow,
      textOverflow: questionStyle.textOverflow,
      whiteSpace: questionStyle.whiteSpace
    };
  });
  expect(compactLayout.flexWrap).toBe("wrap");
  expect(compactLayout.minWidth).toBe("160px");
  expect(compactLayout.overflow).toBe("visible");
  expect(compactLayout.textOverflow).toBe("clip");
  expect(compactLayout.whiteSpace).toBe("normal");

  await expect(page.getByRole("link", { name: "Workbench 6 needs you" })).toBeVisible();
  await expect(decisions).toHaveCount(6);
  await photograph(page, "workbench-bounded-decisions", true);
});

test("proves(a-decision-opens-on-the-workbench-while-you-watch): a decision that opens while you watch appears at 1280 and 390 without a reload", async ({
  page
}) => {
  test.setTimeout(120_000);

  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });

  const schema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"type":"boolean"}'
  });
  expect([200, 201]).toContain(schema.status());
  const schemaRevisionHash = (await schema.json()).schema_revision_hash as string;
  const question = "May this wait open while you watch?";
  const runId = "workbench/live-attention-while-watching";

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
  const card = page.locator(".pinned-decision").filter({ hasText: question });
  await expect(card).toHaveCount(0);
  const openedUrl = page.url();

  const workflow = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: Opened while watching",
      "nodes:",
      "  - id: ask",
      "    type: wait",
      `    prompt: ${question}`,
      `    outputs: [{name: answer, schema: {ref: decision, revision: ${schemaRevisionHash}}}]`,
      ""
    ].join("\n")
  });
  expect(workflow.status()).toBe(201);
  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: (await workflow.json()).workflow_revision_hash as string,
      agent_bindings: [],
      orders: []
    }
  });
  expect(started.status()).toBe(201);

  await expect(card).toBeVisible({ timeout: 20_000 });
  expect(page.url()).toBe(openedUrl);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(card).toBeVisible();
  expect(page.url()).toBe(openedUrl);
});
