import { describe, expect, it, vi } from "vitest";

import type { NodeDetail, RunV1, RunV3 } from "../../src/api/client";
import { meaningOf, ordersInJob } from "../../src/lib/historyMeaning";
import type { HistoryRow } from "../../src/lib/historyRows";
import type { WorkflowGraphV3 } from "../../src/lib/runList";
import { cockpitApiStub } from "../support/cockpitApi";
import { notCancellableBlock } from "../support/runV3";
import { completedRun, publicReference, revisionHash } from "../support/workflowV1";

function graph(previews: WorkflowGraphV3["node_previews"]): WorkflowGraphV3 {
  return {
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    node_count: previews.length,
    agent_roles: ["builder"],
    orders: [],
    wait_answer_schemas: [],
    node_previews: previews,
    loops: [],
    name: "a workflow",
    description: null
  };
}

function v3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/run",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "sink",
    node_rail: [{ node_id: "sink", state: "succeeded", attempt: null }],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: revisionHash,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: "2026-08-18T15:38:00Z",
    ...changes
  };
}

function completedRow(changes: Partial<RunV3> = {}): HistoryRow {
  const run = v3Run(changes);
  return { run, name: "a run", result: { kind: "completed" }, span: null, activityAt: null };
}

function failedRow(nodeId: string, changes: Partial<RunV1> = {}): HistoryRow {
  const run: RunV1 = { ...completedRun(changes), state: "FAILED" };
  return { run, name: "a run", result: { kind: "failed", nodeId }, span: null, activityAt: null };
}

function nodeDetail(nodeId: string, changes: Partial<NodeDetail> = {}): NodeDetail {
  return {
    run_id: "irrelevant",
    public_run_reference: publicReference,
    node_id: nodeId,
    state: "succeeded",
    job_base64: null,
    job_hash: null,
    answer: null,
    provenance: null,
    refusal: null,
    started_at: null,
    ended_at: null,
    ...changes
  };
}

function base64(text: string): string {
  return btoa(text);
}

describe("reading a node's job back into the orders it carries", () => {
  it("finds an order's value under its own heading, past the instruction it follows", () => {
    const job = "Do the thing.\n\n--- order: message ---\nFix the flaky CI";

    expect(ordersInJob(job)).toEqual(new Map([["message", "Fix the flaky CI"]]));
  });

  it("stops an order's value at the result heading that follows it", () => {
    const job =
      "Do the thing.\n\n--- order: message ---\nFix the CI\n\n--- result of build: output ---\nbuilt";

    expect(ordersInJob(job)).toEqual(new Map([["message", "Fix the CI"]]));
  });

  it("finds nothing in a job that reads no order", () => {
    expect(ordersInJob("Just the instruction.")).toEqual(new Map());
  });
});

describe("a row's live purpose and result", () => {
  it("reads the root's order and the sink's answer off the same single node", async () => {
    const single = graph([
      { id: "sink", kind: "agent", role: "builder", instruction_start: "Do it.", depends_on: [] }
    ]);
    const getNodeDetail = vi.fn(async (_reference: string, nodeId: string) =>
      nodeDetail(nodeId, {
        job_base64: base64("Do it.\n\n--- order: message ---\nFix the flaky CI"),
        answer: { value_base64: base64(JSON.stringify("PR #512 merged")), value_hash: "d".repeat(64) }
      })
    );

    const meaning = await meaningOf(completedRow(), single, cockpitApiStub({ getNodeDetail }));

    expect(meaning).toEqual({ purpose: "Fix the flaky CI", result: "PR #512 merged" });
    // The root and the sink are the same node here -- read once, not twice.
    expect(getNodeDetail).toHaveBeenCalledTimes(1);
  });

  it("reads the conductor's own brief envelope down to the operator's message, not the whole order", async () => {
    const single = graph([
      { id: "sink", kind: "agent", role: "conductor", instruction_start: "Read the brief.", depends_on: [] }
    ]);
    // `conductorBrief` (`conductorEpisode.ts`) sends one order whose value is
    // the whole envelope -- message, prior transcript, drop count -- never a
    // bare string.
    const brief = JSON.stringify({
      message: "Fix the flaky CI",
      prior_transcript: [],
      dropped_oldest_messages: 0
    });
    const getNodeDetail = vi.fn(async (_reference: string, nodeId: string) =>
      nodeDetail(nodeId, { job_base64: base64(`Read the brief.\n\n--- order: message ---\n${brief}`) })
    );

    const meaning = await meaningOf(completedRow(), single, cockpitApiStub({ getNodeDetail }));

    expect(meaning.purpose).toBe("Fix the flaky CI");
  });

  it("reads a single-field report object down to that field's own text, whatever it is named", async () => {
    const single = graph([
      { id: "sink", kind: "agent", role: "builder", instruction_start: "Do it.", depends_on: [] }
    ]);
    const getNodeDetail = vi.fn(async (_reference: string, nodeId: string) =>
      nodeDetail(nodeId, {
        // The conductor's report field is `answer`; another workflow's report
        // (`greeting`, or any other name) reads the same way -- structurally,
        // never by a hardcoded field name.
        answer: {
          value_base64: base64(JSON.stringify({ greeting: "the workflow could not be started (409)" })),
          value_hash: "d".repeat(64)
        }
      })
    );

    const meaning = await meaningOf(completedRow(), single, cockpitApiStub({ getNodeDetail }));

    expect(meaning.result).toBe("the workflow could not be started (409)");
  });

  it("reads the one string field a multi-field report carries, past its non-string siblings", async () => {
    const single = graph([
      { id: "sink", kind: "agent", role: "builder", instruction_start: "Do it.", depends_on: [] }
    ]);
    const getNodeDetail = vi.fn(async (_reference: string, nodeId: string) =>
      nodeDetail(nodeId, {
        answer: {
          value_base64: base64(
            JSON.stringify({ answer: "Started hello-atelier.", started_run_ids: ["hello-atelier-1"] })
          ),
          value_hash: "d".repeat(64)
        }
      })
    );

    const meaning = await meaningOf(completedRow(), single, cockpitApiStub({ getNodeDetail }));

    expect(meaning.result).toBe("Started hello-atelier.");
  });

  it("falls back to the compact JSON line when no single field reads unambiguously", async () => {
    const single = graph([
      { id: "sink", kind: "agent", role: "builder", instruction_start: "Do it.", depends_on: [] }
    ]);
    const getNodeDetail = vi.fn(async (_reference: string, nodeId: string) =>
      nodeDetail(nodeId, {
        answer: {
          value_base64: base64(JSON.stringify({ status: "merged", note: "PR #512 merged" })),
          value_hash: "d".repeat(64)
        }
      })
    );

    const meaning = await meaningOf(completedRow(), single, cockpitApiStub({ getNodeDetail }));

    expect(meaning.result).toBe('{"status":"merged","note":"PR #512 merged"}');
  });

  it("names no order when the workflow's own root reads none", async () => {
    const single = graph([
      { id: "sink", kind: "agent", role: "builder", instruction_start: "Do it.", depends_on: [] }
    ]);
    const getNodeDetail = vi.fn(async (_reference: string, nodeId: string) =>
      nodeDetail(nodeId, { answer: { value_base64: base64('"done"'), value_hash: "d".repeat(64) } })
    );

    const meaning = await meaningOf(completedRow(), single, cockpitApiStub({ getNodeDetail }));

    expect(meaning.purpose).toBeNull();
  });

  it("falls back honestly when the sink's own node cannot be read", async () => {
    const single = graph([
      { id: "sink", kind: "agent", role: "builder", instruction_start: "Do it.", depends_on: [] }
    ]);
    const getNodeDetail = vi.fn().mockRejectedValue(new Error("network gone"));

    const meaning = await meaningOf(completedRow(), single, cockpitApiStub({ getNodeDetail }));

    expect(meaning).toEqual({ purpose: null, result: "Result unavailable" });
  });

  it("carries no purpose for a run whose workflow revision could not be resolved", async () => {
    const getNodeDetail = vi.fn(async (_reference: string, nodeId: string) =>
      nodeDetail(nodeId, { refusal: "output-schema-refused" })
    );

    const meaning = await meaningOf(failedRow("final"), null, cockpitApiStub({ getNodeDetail }));

    expect(meaning).toEqual({ purpose: null, result: "output-schema-refused" });
  });

  it("names a failed run's own refusal sentence", async () => {
    const getNodeDetail = vi.fn(async (_reference: string, nodeId: string) =>
      nodeDetail(nodeId, { state: "failed", refusal: "conduct: output-schema-refused: instance-not-json" })
    );

    const meaning = await meaningOf(failedRow("conduct"), null, cockpitApiStub({ getNodeDetail }));

    expect(meaning.result).toBe("conduct: output-schema-refused: instance-not-json");
  });

  it("names the node it stopped at when the refusal itself cannot be read", async () => {
    const getNodeDetail = vi.fn().mockRejectedValue(new Error("network gone"));

    const meaning = await meaningOf(failedRow("review"), null, cockpitApiStub({ getNodeDetail }));

    expect(meaning.result).toBe("review");
  });
});
