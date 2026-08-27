import { expect, test, type Page, type Route } from "@playwright/test";
import { existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

import {
  encodePublicRunReference,
  type NodeDetail,
  type RunV3,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { runPageCopy, usageLine } from "../../src/lib/runPageCopy";
import { notCancellableBlock } from "../support/runV3";

/**
 * #666 Log tab against the blessed picture frame `#v8-14-run-log`.
 *
 * Mockup shots (gitignored, taken from that frame):
 *   test-results/log-666/mockup-{1280,390}-{light,dark}.png
 * Result shots of this journey, same viewports and themes:
 *   test-results/log-666/result-{1280,390}-{light,dark}.png
 * Pixel-perfect mismatch is not a failure: the mockup is the whole room with
 * fixture copy; the app has real graph chrome.
 */
test.setTimeout(180_000);

const revisionHash = "a".repeat(64);
const publicReference = encodePublicRunReference("v3/log-tab");
const PLANTED_CANARY = "sk-ant" + "-plantedcanarysecret0123456789";
const frontendRoot = resolve(import.meta.dirname, "../..");
const shotDir = resolve(frontendRoot, "test-results/log-666");
const mockupHtml = resolve(
  frontendRoot,
  "../docs/requirements/0003-ziel-ui-mockup-v8.html"
);

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;
const themes = ["light", "dark"] as const;

function failedRun(): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/log-tab",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 2,
    state: "FAILED",
    current_node_id: "reviewer",
    node_rail: [
      { node_id: "planner", state: "succeeded", attempt: null },
      { node_id: "builder", state: "succeeded", attempt: null },
      { node_id: "reviewer", state: "failed", attempt: { ordinal: 2, state: "FAILED" } }
    ],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: "d".repeat(64),
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: "2026-08-18T15:00:10Z"
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
      node_count: 3,
      agent_roles: ["builder"],
      orders: [],
      wait_answer_schemas: [],
      node_previews: [
        {
          id: "planner",
          kind: "agent",
          role: "builder",
          instruction_start: "Name the work.",
          depends_on: []
        },
        {
          id: "builder",
          kind: "agent",
          role: "builder",
          instruction_start: "Do the one thing this chain is for.",
          depends_on: ["planner"]
        },
        {
          id: "reviewer",
          kind: "agent",
          role: "builder",
          instruction_start: "Check the changed files against the brief.",
          depends_on: ["builder"]
        }
      ],
      loops: [],
      name: "Sweep the docs",
      description: null
    }
  };
}

function nodeBase(nodeId: string, state: NodeDetail["state"]): Omit<NodeDetail, "transcript"> {
  return {
    run_id: "v3/log-tab",
    public_run_reference: publicReference,
    node_id: nodeId,
    state,
    job_base64: btoa("Check the changed files."),
    job_hash: "e".repeat(64),
    answer: state === "succeeded" ? { value_base64: btoa("Done."), value_hash: "f".repeat(64) } : null,
    provenance: {
      role: "builder",
      provider_id: "e2e-v3",
      model: "grok-4.6",
      executor_revision: "immediate/v1",
      executor_operational_identity: "e2e-immediate-process",
      auth_mode: "subscription",
      profile_id: "shots",
      agent_configuration_revision_hash: "a".repeat(64),
      request_hash: "b".repeat(64),
      receipt_hash: "c".repeat(64)
    },
    refusal: state === "failed" ? "The last attempt stopped while checking the changed files." : null,
    refusal_output: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: "2026-08-18T15:00:10Z"
  };
}

function reviewerDetail(): NodeDetail {
  return {
    ...nodeBase("reviewer", "failed"),
    transcript: {
      events: [
        {
          event: "assistant-turn",
          text: "I will check the changed files against the review brief.",
          redacted: false
        },
        {
          event: "tool-called",
          name: "Read",
          arguments: '{"path":"docs/requirements/0003-ziel-ui.md"}',
          redacted: false
        },
        {
          event: "tool-returned",
          name: "Read",
          result: "Read 128 lines.",
          redacted: false
        },
        {
          event: "assistant-turn",
          text: "The acceptance sentence and the picture still disagree.",
          redacted: false
        },
        {
          event: "tool-called",
          name: "Bash",
          arguments: '{"command":"check changed files"}',
          redacted: false
        },
        {
          event: "tool-returned",
          name: "Bash",
          result: "Command ended before it returned an answer.",
          redacted: false
        },
        {
          event: "usage",
          input_tokens: 12_400,
          output_tokens: 680,
          cache_read_input_tokens: 0,
          cache_creation_input_tokens: 0
        },
        {
          event: "unrecognised-provider-output",
          text: "checking changed files\ncanary credential: [redacted]\ncommand stopped before an answer",
          redacted: true
        }
      ]
    }
  };
}

function endedWithoutTranscript(nodeId: string): NodeDetail {
  return { ...nodeBase(nodeId, "succeeded"), transcript: null };
}

async function routeRun(page: Page): Promise<void> {
  const run = failedRun();
  await page.route(`**/atelier/api/v1/runs/${publicReference}`, async (route: Route) => {
    await route.fulfill({ json: run });
  });
  await page.route(`**/atelier/api/v1/runs/${publicReference}/events`, async () => {
    // Leave the stream connecting so the calm surface carries no reconnect line.
  });
  await page.route(`**/atelier/api/v1/workflow-revisions/${revisionHash}`, async (route: Route) => {
    await route.fulfill({ json: revision() });
  });
  await page.route(
    `**/atelier/api/v1/runs/${publicReference}/nodes/**`,
    async (route: Route) => {
      const url = route.request().url();
      const nodeId = decodeURIComponent(url.split("/nodes/").pop() ?? "");
      if (nodeId === "reviewer") {
        await route.fulfill({ json: reviewerDetail() });
        return;
      }
      await route.fulfill({ json: endedWithoutTranscript(nodeId || "planner") });
    }
  );
}

async function shoot(page: Page, name: string): Promise<void> {
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.waitForTimeout(200);
      await page.getByRole("complementary").screenshot({
        path: `${shotDir}/${name}-${viewport.name}-${theme}.png`
      });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

async function shootMockupFrame(page: Page): Promise<void> {
  mkdirSync(shotDir, { recursive: true });
  for (const theme of themes) {
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.emulateMedia({ colorScheme: theme });
      await page.goto(`file://${mockupHtml}`);
      await page.evaluate((nextTheme) => {
        document.documentElement.setAttribute("data-theme", nextTheme);
      }, theme);
      const frame = page.locator("#v8-14-run-log");
      await frame.waitFor({ state: "visible" });
      await frame.screenshot({ path: `${shotDir}/mockup-${viewport.name}-${theme}.png` });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });
}

test("the Log tab shows the stored transcript, redacts the canary, and photographs both widths", async ({
  page
}) => {
  await shootMockupFrame(page);
  await routeRun(page);
  await page.goto(`/atelier/runs/${publicReference}`);
  await expect(page.getByRole("heading", { level: 1, name: "Sweep the docs" })).toBeVisible({
    timeout: 30_000
  });

  await page.getByRole("button", { name: "reviewer — Failed" }).click();
  const panel = page.getByRole("complementary");
  await expect(panel.getByRole("heading", { name: "reviewer" })).toBeVisible();
  await panel.getByRole("tab", { name: runPageCopy.tabLog }).click();

  const transcript = panel.getByRole("region", { name: runPageCopy.transcriptRegion });
  await expect(transcript).toBeVisible();
  await expect(transcript.getByText(runPageCopy.assistantTurn).first()).toBeVisible();
  await expect(transcript.getByText("I will check the changed files against the review brief.")).toBeVisible();
  await expect(transcript.getByText(runPageCopy.doorCall).first()).toBeVisible();
  await expect(transcript.getByText("Read", { exact: true })).toBeVisible();
  await expect(transcript.getByText(runPageCopy.doorAnswer).first()).toBeVisible();
  await expect(transcript.getByText("Read 128 lines.")).toBeVisible();
  await expect(transcript.getByText(runPageCopy.usage)).toBeVisible();
  await expect(transcript.getByText(usageLine(12_400, 680, "10 s"))).toBeVisible();
  await expect(transcript.getByText(runPageCopy.attemptStdout)).toBeVisible();
  await expect(transcript.getByText("Failed").first()).toBeVisible();
  await expect(transcript.getByText(runPageCopy.redacted)).toBeVisible();
  await expect(page.getByText(PLANTED_CANARY)).toHaveCount(0);
  await expect(transcript.getByText(/checking changed files/)).toBeVisible();

  const folded = transcript.locator("details").first();
  await expect(folded).not.toHaveAttribute("open");
  await folded.locator("summary").click();
  await expect(transcript.getByText('{"path":"docs/requirements/0003-ziel-ui.md"}')).toBeVisible();
  await folded.locator("summary").click();
  await expect(folded).not.toHaveAttribute("open");

  await shoot(page, "result");

  for (const theme of themes) {
    for (const viewport of widths) {
      const mockup = `${shotDir}/mockup-${viewport.name}-${theme}.png`;
      const result = `${shotDir}/result-${viewport.name}-${theme}.png`;
      expect(existsSync(result), `missing result shot ${result}`).toBe(true);
      expect(existsSync(mockup), `missing mockup shot ${mockup}`).toBe(true);
    }
  }
});
