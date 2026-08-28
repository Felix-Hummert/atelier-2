import { describe, expect, it } from "vitest";

import type { RunV3 } from "../../src/api/client";
import {
  applyAttentionFrame,
  markAttentionConnecting,
  markAttentionLive,
  startAttentionHold
} from "../../src/lib/attentionHold";
import {
  absorbAttentionRun,
  workbenchDecisionPins
} from "../../src/lib/workbenchAttention";
import { notCancellableBlock } from "../support/runV3";
import { publicReference, revisionHash, waitingInput } from "../support/workflowV1";

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
    cancellation: notCancellableBlock("between-nodes"),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...changes
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

  it("keeps the open decision through a drop and still takes the next one after recover", () => {
    const first = waitingRun({ public_run_reference: "run1.YQ" });
    let runs = absorbAttentionRun([], first);
    let hold = markAttentionLive(startAttentionHold());

    hold = markAttentionConnecting(hold, true);
    expect(hold.connection).toBe("reconnecting");
    expect(workbenchDecisionPins(runs)).toEqual([first]);

    hold = markAttentionLive(hold);
    expect(hold.connection).toBe("live");
    const applied = applyAttentionFrame(hold, JSON.stringify(waitingInput(1)));
    expect(applied.hold.protocol_problem).toBeNull();
    expect(applied.event?.event).toBe("WAITING_INPUT");

    const recovered = waitingRun({ public_run_reference: "run1.Yg", run_id: "after recover" });
    runs = absorbAttentionRun(runs, recovered);
    expect(workbenchDecisionPins(runs)).toEqual([first, recovered]);
  });
});
