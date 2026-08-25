import { test, type Page, type Route } from "@playwright/test";

import { encodePublicRunReference, type RunV3, type WorkflowRevisionDetail } from "../../src/api/client";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { cancellableBlock, notCancellableBlock } from "../support/runV3";

/**
 * Evidence screenshots of the #439 P5 cancel control in its four states, at both
 * widths and in both themes. Not a gate: it is skipped unless
 * ATELIER2_CANCEL_SHOT_DIR names where the images go.
 */
const shotDir = process.env.ATELIER2_CANCEL_SHOT_DIR ?? "";

test.skip(shotDir === "", "no cancel shot directory named");
test.setTimeout(180_000);

const revisionHash = "a".repeat(64);
const publicReference = encodePublicRunReference("v3/cancel");
const targetNodeExecutionId = "d".repeat(64);

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;
const themes = ["light", "dark"] as const;

function baseRun(): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/cancel",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 2,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [
      { node_id: "implement", state: "succeeded", attempt: null },
      { node_id: "review", state: "working", attempt: null }
    ],
    cancellation: cancellableBlock(targetNodeExecutionId),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: new Date(Date.now() - 90_000).toISOString(),
    ended_at: null
  };
}

function revision(): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 2,
      agent_roles: ["builder"],
      orders: [],
      wait_answer_schemas: [],
      node_previews: [
        {
          id: "implement",
          kind: "agent",
          role: "builder",
          instruction_start: "Do the one thing this chain is for.",
          depends_on: []
        },
        {
          id: "review",
          kind: "agent",
          role: "builder",
          instruction_start: "Check what the node before you did.",
          depends_on: ["implement"]
        }
      ],
      loops: [],
      name: "Two agents in a line",
      description: null
    }
  };
}

async function routeRun(page: Page, run: RunV3, cancel: "accept" | "fail" = "accept"): Promise<void> {
  await page.route(`**/atelier/api/v1/runs/${publicReference}`, async (route: Route) => {
    await route.fulfill({ json: run });
  });
  await page.route(`**/atelier/api/v1/runs/${publicReference}/events`, async () => {
    // Leave the stream connecting so the calm surface carries no reconnect line.
  });
  await page.route(`**/atelier/api/v1/workflow-revisions/${revisionHash}`, async (route: Route) => {
    await route.fulfill({ json: revision() });
  });
  await page.route(`**/atelier/api/v1/runs/${publicReference}/cancellations`, async (route: Route) => {
    if (cancel === "fail") {
      await route.abort();
      return;
    }
    await route.fulfill({
      status: 202,
      json: { ...run, cancellation: notCancellableBlock("already-cancelling") }
    });
  });
}

async function shoot(page: Page, name: string): Promise<void> {
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.waitForTimeout(200);
      await page.screenshot({ path: `${shotDir}/${theme}/${name}-${viewport.name}.png`, fullPage: true });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

test("cancel control states", async ({ page }) => {
  const cancel = runPageCopy.cancel;

  // 1. A cancelable run showing the control.
  await routeRun(page, baseRun());
  await page.goto(`/atelier/runs/${publicReference}`);
  await page.getByRole("button", { name: cancel.open }).waitFor();
  await shoot(page, "cancelable");

  // 2. The staged cancel decision.
  await page.getByRole("button", { name: cancel.open }).click();
  await page.getByRole("heading", { name: cancel.question }).waitFor();
  await shoot(page, "staged-decision");

  // 3. The confirmed state: the run is stopping.
  await page.getByRole("button", { name: cancel.confirm }).click();
  await page.getByText(cancel.accepted).first().waitFor();
  await shoot(page, "confirmed");

  // 4. A reload during a cancel the server never confirmed: the card reads
  //    "Cancel uncertain" with Retry/Discard, never a false "Stopping this run".
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.evaluate(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  await routeRun(page, baseRun(), "fail");
  await page.goto(`/atelier/runs/${publicReference}`);
  await page.getByRole("button", { name: cancel.open }).click();
  await page.getByRole("button", { name: cancel.confirm }).click();
  await page.getByText(cancel.uncertain).first().waitFor();
  // Reload: the durable journal, not the lost reply, decides what the card says.
  await page.goto(`/atelier/runs/${publicReference}`);
  await page.getByRole("button", { name: cancel.retry }).waitFor();
  await shoot(page, "uncertain-reload");

  // 5. A non-cancelable run showing its reason. Clear the journalled cancel
  //    first, so this run reads as a fresh non-cancelable one.
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.evaluate(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  await routeRun(page, { ...baseRun(), cancellation: notCancellableBlock("between-nodes") });
  await page.goto(`/atelier/runs/${publicReference}`);
  await page.getByText(/No agent is running that this cancel could stop/).waitFor();
  await shoot(page, "not-cancelable");
});
