import { expect, test, type Page, type Route } from "@playwright/test";

import { encodePublicRunReference, type RunV3, type WorkflowRevisionDetail } from "../../src/api/client";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { cancellableBlock } from "../support/runV3";

/**
 * Chromium proof for #572: a boolean wait gate answers on its own Board
 * card, in two clicks, at both the desktop and the phone width the workshop
 * ships for.
 */

const revisionHash = "a".repeat(64);
const publicReference = encodePublicRunReference("v3/board-inline");

const viewports = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;

function waitingRun(overrides: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/board-inline",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "WAITING_INPUT",
    current_node_id: "approve",
    node_rail: [{ node_id: "approve", state: "needs_you", attempt: null }],
    // A resting Wait is operator-cancellable (#668): ending it there is the
    // wait's own honest exit, not a state the Board card has to explain.
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: new Date().toISOString(),
    ended_at: null,
    ...overrides
  };
}

function answeredRun(): RunV3 {
  return waitingRun({
    state: "COMPLETED",
    terminal_hash: "d".repeat(64),
    node_rail: [{ node_id: "approve", state: "succeeded", attempt: null }],
    ended_at: new Date().toISOString()
  });
}

function revision(kind: "boolean" | "free"): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: [],
      orders: [],
      wait_answer_schemas: [
        { node_id: "approve", schema: { ref: "decision", revision: "e".repeat(64) }, kind, values: null }
      ],
      node_previews: [
        { id: "approve", kind: "wait", role: null, instruction_start: null, depends_on: [] }
      ],
      loops: [],
      name: "Approve once",
      description: null
    }
  } as WorkflowRevisionDetail;
}

async function mockAttentionOpen(page: Page): Promise<void> {
  await page.addInitScript(() =>
    Object.defineProperty(window, "EventSource", {
      value: class extends EventTarget {
        constructor() {
          super();
          queueMicrotask(() => this.dispatchEvent(new Event("open")));
        }
        close() {}
      }
    })
  );
}

async function routeBoard(page: Page, state: { run: RunV3; kind: "boolean" | "free" }): Promise<void> {
  await page.route("**/atelier/api/v1/runs*", async (route: Route) => {
    const url = new URL(route.request().url());
    const requested = url.searchParams.get("state");
    const items = requested === null || requested === state.run.state ? [state.run] : [];
    await route.fulfill({ json: { items, next_after: null } });
  });
  await page.route(`**/atelier/api/v1/workflow-revisions/${revisionHash}`, async (route: Route) => {
    await route.fulfill({ json: revision(state.kind) });
  });
  await page.route(`**/atelier/api/v1/runs/${publicReference}/answers`, async (route: Route) => {
    state.run = answeredRun();
    await route.fulfill({ status: 200, json: state.run });
  });
}

test("proves(a-boolean-board-card-answers-inline): a boolean wait gate answers on its own Board card in two clicks, at desktop and phone width", async ({
  page
}) => {
  await mockAttentionOpen(page);

  for (const viewport of viewports) {
    const state = { run: waitingRun(), kind: "boolean" as const };
    await routeBoard(page, state);
    await page.setViewportSize(viewport);
    await page.goto("/atelier");

    const needsYou = page.getByRole("region", { name: "Needs you · 1" });
    await expect(needsYou).toBeVisible();
    const toggle = needsYou.getByRole("button", { name: /Answer here/ });
    await expect(toggle).toBeVisible();
    await expect(needsYou.getByRole("button", { name: runPageCopy.answerYes })).toHaveCount(0);

    await toggle.click();
    const yes = needsYou.getByRole("button", { name: runPageCopy.answerYes });
    await expect(yes).toBeVisible();
    await page.screenshot({
      path: `test-results/board-inline-answer-expanded-${viewport.name}.png`,
      fullPage: true
    });

    await yes.click();

    // The row leaving Needs you and disappearing from the Board is the
    // visible confirmation of the send -- no separate banner to word
    // (operator ruling 23.08.). The answered run turned terminal, so it
    // moves to History instead of landing in a "Done" group here (#667).
    await expect(page.getByRole("region", { name: /Needs you/ })).toHaveCount(0);
    await expect(page.getByRole("region", { name: /Running/ })).toHaveCount(0);
    await page.screenshot({
      path: `test-results/board-inline-answer-confirmed-${viewport.name}.png`,
      fullPage: true
    });
  }
});

test("proves(a-free-text-board-card-links-to-the-run-page): a free-text wait gate names honestly that it needs a written answer instead of offering buttons it cannot answer here", async ({
  page
}) => {
  await mockAttentionOpen(page);
  const state = { run: waitingRun(), kind: "free" as const };
  await routeBoard(page, state);

  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier");

    const needsYou = page.getByRole("region", { name: "Needs you · 1" });
    await needsYou.getByRole("button", { name: /Answer here/ }).click();

    await expect(needsYou.getByText("This needs a written answer.")).toBeVisible();
    await expect(needsYou.getByRole("button", { name: runPageCopy.answerYes })).toHaveCount(0);
    const open = needsYou.getByRole("link", { name: "Open the run to answer" });
    await expect(open).toBeVisible();
    await page.screenshot({
      path: `test-results/board-inline-answer-free-text-${viewport.name}.png`,
      fullPage: true
    });

    await open.click();
    await expect(page).toHaveURL(new RegExp(`/atelier/runs/${publicReference}$`));
  }
});
