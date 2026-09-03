import { describe, expect, it } from "vitest";

import type { RunV3 } from "../../src/api/client";
import { forkFactList, planRunFork } from "../../src/lib/runFork";
import { cancellableBlock, notCancellableBlock } from "../support/runV3";
import { publicReference, revisionHash as digest } from "../support/runV3";

function origin(overrides: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/two-agents",
    workflow_name: "Two agents in a line",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [
      {
        name: "work_item",
        bytes: 48,
        schema_revision_hash: digest
      }
    ],
    state_version: 1,
    state: "FAILED",
    current_node_id: "review",
    current_node_execution_id: "e".repeat(64),
    node_rail: [
      { node_id: "implement", state: "succeeded", attempt: null },
      { node_id: "review", state: "failed", attempt: null }
    ],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: "d".repeat(64),
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: "2026-08-18T15:00:12Z",
    ...overrides
  };
}

describe("retry-from-node projection", () => {
  it("names the prefix as carried over and the restart node onward as running again, with order names only", () => {
    const planned = planRunFork(origin(), "review");

    expect(planned).toEqual({
      kind: "ok",
      restartFrom: "review",
      carriedNodeIds: ["implement"],
      rerunNodeIds: ["review"],
      orderNames: ["work_item"]
    });
    expect(forkFactList(planned.kind === "ok" ? planned.orderNames : [])).toBe("work_item");
    expect(JSON.stringify(planned)).not.toContain("secret");
    expect(JSON.stringify(planned)).not.toMatch(/bytes":48/);
  });

  it("carries no prefix nodes when the restart is the first node, and still lists recorded order names", () => {
    expect(planRunFork(origin(), "implement")).toEqual({
      kind: "ok",
      restartFrom: "implement",
      carriedNodeIds: [],
      rerunNodeIds: ["implement", "review"],
      orderNames: ["work_item"]
    });
  });

  it("refuses a running run", () => {
    expect(planRunFork(origin({ state: "STARTED", cancellation: cancellableBlock() }), "review")).toEqual({
      kind: "running"
    });
  });

  it("refuses a run that is waiting for input", () => {
    expect(
      planRunFork(origin({ state: "WAITING_INPUT", cancellation: cancellableBlock() }), "review")
    ).toEqual({ kind: "running" });
  });

  it("refuses a run that is waiting for reconciliation", () => {
    expect(
      planRunFork(
        origin({ state: "WAITING_RECONCILIATION", cancellation: cancellableBlock() }),
        "review"
      )
    ).toEqual({ kind: "running" });
  });

  it("refuses a node that is not on the rail", () => {
    expect(planRunFork(origin(), "merge")).toEqual({ kind: "unknown-node" });
  });

  it("refuses a restart whose prefix is not succeeded", () => {
    expect(
      planRunFork(
        origin({
          node_rail: [
            { node_id: "implement", state: "failed", attempt: null },
            { node_id: "review", state: "queued", attempt: null }
          ]
        }),
        "review"
      )
    ).toEqual({ kind: "prefix-not-reusable" });
  });
});
