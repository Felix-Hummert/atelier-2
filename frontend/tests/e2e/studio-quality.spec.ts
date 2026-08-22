import { expect, test, type Page, type Route } from "@playwright/test";

import { encodePublicRunReference, runPageSchema, type RunV1, type RunV3 } from "../../src/api/client";
import { humanMove } from "../../src/lib/runState";
import { connectionLabels } from "../../src/lib/streamStatus";
import { studioPageCopy } from "../../src/lib/studioPageCopy";
import {
  describeStudioControlFacts,
  questionForStudioControlFacts,
  studioInteractiveSelector,
  studioQuestions,
  studioStageSelector,
  type StudioControlFacts
} from "../../src/lib/studioQuestions";
import {
  completedRun,
  revisionHash,
  startedRun,
  waitingInputRun,
  waitingReconciliationRun
} from "../support/workflowV1";

const studioViewports = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;

function wrapped(text: string): string {
  return `[[[ ${text} ]]]`;
}

function requiredMove(state: RunV1["state"]): string {
  const move = humanMove(state);
  if (move === null) {
    throw new Error(`${state} must name a human move`);
  }
  return move;
}

function listedRun(runId: string, factory: (changes?: Partial<RunV1>) => RunV1, extra: Partial<RunV1> = {}): RunV1 {
  return factory({
    run_id: runId,
    public_run_reference: encodePublicRunReference(runId),
    latest_event_cursor: null,
    ...extra
  });
}

function listedV3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/two-agents",
    public_run_reference: encodePublicRunReference("v3/two-agents"),
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [{ node_id: "review", state: "working", attempt: null }],
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...changes
  };
}

function populatedRuns(): RunV1[] {
  const reconciliation = waitingReconciliationRun();
  if (reconciliation.waiting.type !== "WAITING_RECONCILIATION") {
    throw new Error("waiting reconciliation fixture must wait for reconciliation");
  }
  return [
    listedRun("run-a", startedRun),
    listedRun("run-b", startedRun),
    listedRun("wait-a", waitingInputRun),
    listedRun("wait-b", waitingReconciliationRun, {
      waiting: { ...reconciliation.waiting, node_id: reconciliation.current_node.node_id }
    }),
    listedRun("fail-a", startedRun, { state: "FAILED", terminal_hash: revisionHash }),
    listedRun("done-a", completedRun)
  ];
}

function questionMapRuns(): Array<RunV1 | RunV3> {
  return [
    ...populatedRuns(),
    listedV3Run({
      run_id: "done-v3",
      public_run_reference: encodePublicRunReference("done-v3"),
      state: "COMPLETED",
      terminal_hash: revisionHash,
      node_rail: [{ node_id: "review", state: "succeeded", attempt: null }],
      ended_at: "2026-08-18T12:00:00Z"
    })
  ];
}

type StudioReadReply = "populated" | "unavailable" | "empty" | "questions";

async function routeStudioReads(page: Page, read: () => StudioReadReply): Promise<void> {
  await page.route("**/atelier/api/v1/runs*", async (route: Route) => {
    if (read() === "unavailable") {
      await route.abort();
      return;
    }
    const url = new URL(route.request().url());
    const state = url.searchParams.get("state");
    const source = read() === "empty" ? [] : read() === "questions" ? questionMapRuns() : populatedRuns();
    const items = source.filter((run) => state === null || run.state === state);
    await route.fulfill({ json: { items, next_after: null } });
  });
}

async function expectStudioCopyFits(page: Page): Promise<void> {
  const heading = page.getByRole("heading", { name: wrapped(studioPageCopy.title) });
  const board = page.locator(".board-page");
  expect(await heading.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await board.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
}

async function expectPopulatedCopy(page: Page): Promise<void> {
  const needsYou = page.getByRole("region", { name: `${wrapped(studioPageCopy.needsYou)} · 2` });
  await expect(needsYou).toBeVisible();
  await expect(needsYou.getByText(`${wrapped(requiredMove("WAITING_INPUT"))} →`)).toBeVisible();
  await expect(needsYou.getByText(`${wrapped(requiredMove("WAITING_RECONCILIATION"))} →`)).toBeVisible();

  const running = page.getByRole("region", { name: `${wrapped(studioPageCopy.running)} · 3` });
  await expect(running).toBeVisible();
  await expect(running.getByText(`${wrapped(studioPageCopy.why)} →`)).toBeVisible();

  const done = page.getByRole("region", { name: `${wrapped(studioPageCopy.done)} · 1` });
  await expect(done).toBeVisible();
  await expect(done.getByText(wrapped(studioPageCopy.completedSentence))).toBeVisible();

  await expect(page.getByRole("status").filter({ hasText: wrapped(connectionLabels.live) })).toBeVisible();
}

async function mockAttentionOpen(page: Page): Promise<void> {
  await page.addInitScript(() => Object.defineProperty(window, "EventSource", { value: class extends EventTarget { constructor() { super(); queueMicrotask(() => this.dispatchEvent(new Event("open"))); } close() {} } }));
}

async function expectStudioControlsAnswerNamedQuestions(
  page: Page,
  expected: ReadonlyArray<(typeof studioQuestions)[keyof typeof studioQuestions]["id"]>
): Promise<void> {
  const facts = await page.locator(studioStageSelector).evaluate(
    (root, selector) =>
      [...root.querySelectorAll(selector)].map((element) => ({
        questionId: element.getAttribute("data-studio-question"),
        href: element.getAttribute("href"),
        ariaLabel: element.getAttribute("aria-label"),
        tag: element.tagName.toLowerCase()
      })),
    studioInteractiveSelector
  ) as StudioControlFacts[];
  const unanswered = facts.filter((item) => questionForStudioControlFacts(item) === null);
  expect(
    unanswered.map(describeStudioControlFacts),
    unanswered.map(describeStudioControlFacts).join("; ")
  ).toEqual([]);
  expect(new Set(facts.map((item) => questionForStudioControlFacts(item)?.id))).toEqual(
    new Set(expected)
  );
}


test("proves(studio-populated-copy-is-owned-and-survives-pseudo-locale): Studio keeps populated and unavailable copy visible at desktop and 390px", async ({ page }) => {
  expect(runPageSchema.safeParse({ items: populatedRuns(), next_after: null }).success).toBe(true);
  await page.addInitScript(() => Object.defineProperty(window, "EventSource", { value: class extends EventTarget { constructor() { super(); queueMicrotask(() => this.dispatchEvent(new Event("open"))); } close() {} } }));
  let reply: StudioReadReply = "populated";
  await routeStudioReads(page, () => reply);

  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    reply = "populated";
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByRole("heading", { name: wrapped(studioPageCopy.title) })).toBeVisible();
    await expectPopulatedCopy(page);
    await expectStudioCopyFits(page);
    await page.screenshot({ path: `test-results/studio-populated-${viewport.width}.png`, fullPage: true });
  }

  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    reply = "unavailable";
    await page.goto("/atelier?pseudo-locale=1");
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByRole("heading", { name: wrapped(studioPageCopy.title) })).toBeVisible();
    await expect(page.getByText(wrapped(studioPageCopy.runsUnavailable))).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: wrapped(connectionLabels.live) })).toBeVisible();
    await expectStudioCopyFits(page);
    await page.screenshot({ path: `test-results/studio-unavailable-${viewport.width}.png`, fullPage: true });
  }
});

test("proves(studio-elements-answer-named-questions): every interactive Studio control answers one named user question on populated and empty Studio", async ({ page }) => {
  expect(runPageSchema.safeParse({ items: questionMapRuns(), next_after: null }).success).toBe(true);
  await mockAttentionOpen(page);
  let reply: StudioReadReply = "questions";
  await routeStudioReads(page, () => reply);

  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    reply = "questions";
    await page.goto("/atelier");
    await expect(page.getByRole("heading", { name: studioPageCopy.title })).toBeVisible();
    await expect(page.getByRole("region", { name: "Done · 2" })).toBeVisible();
    await expect(page.getByRole("link", { name: studioPageCopy.start })).toBeVisible();
    await expectStudioControlsAnswerNamedQuestions(page, [
      studioQuestions.start.id,
      studioQuestions.openRun.id,
      studioQuestions.reloadStudioRuns.id
    ]);
  }

  for (const viewport of studioViewports) {
    await page.setViewportSize(viewport);
    reply = "empty";
    await page.goto("/atelier");
    await expect(page.getByRole("heading", { name: studioPageCopy.emptyTitle })).toBeVisible();
    await expect(page.getByRole("link", { name: studioPageCopy.emptyStart })).toBeVisible();
    await expectStudioControlsAnswerNamedQuestions(page, [
      studioQuestions.emptyStart.id,
      studioQuestions.reloadStudioRuns.id
    ]);
  }
});
