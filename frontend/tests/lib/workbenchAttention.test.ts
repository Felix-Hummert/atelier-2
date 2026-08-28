import { describe, expect, it } from "vitest";

import type { RunV3 } from "../../src/api/client";
import {
  absorbAttentionRun,
  workbenchDecisionPins
} from "../../src/lib/workbenchAttention";
import { cancellableBlock } from "../support/runV3";
import { publicReference, revisionHash } from "../support/workflowV1";

function waitingRun(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/decide",
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
    // A resting Wait is operator-cancellable (#668).
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...changes,
    current_node_execution_id: changes.current_node_execution_id ?? revisionHash
  };
}

describe("the Workbench's live attention projection", () => {
  it("pins a decision that opens while the operator is looking", () => {
    const opened = waitingRun({ run_id: "opened while here" });

    const runs = absorbAttentionRun([], opened);

    expect(workbenchDecisionPins(runs)).toEqual([opened]);
  });

  it("retires a decision that was answered elsewhere, leaving the surface empty", () => {
    const opened = waitingRun();
    const answered = waitingRun({
      state: "COMPLETED",
      state_version: 2,
      terminal_hash: "d".repeat(64),
      node_rail: [{ node_id: "approve", state: "succeeded", attempt: null }],
      ended_at: "2026-08-18T15:01:00Z"
    });

    const runs = absorbAttentionRun(absorbAttentionRun([], opened), answered);

    expect(workbenchDecisionPins(runs)).toEqual([]);
    expect(runs).toEqual([]);
  });

  it("keeps one card when the same decision arrives twice", () => {
    const first = waitingRun({ state_version: 1 });
    const again = waitingRun({ state_version: 2 });

    const runs = absorbAttentionRun(absorbAttentionRun([], first), again);

    expect(workbenchDecisionPins(runs)).toEqual([again]);
  });

});
