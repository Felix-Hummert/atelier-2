import { describe, expect, it } from "vitest";

import type { RunV3 } from "../../src/api/client";
import { projectBoardGroups } from "../../src/lib/boardRows";
import { notCancellableBlock } from "../support/runV3";

function v3Run(state: RunV3["state"], runId: string): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: runId,
    public_run_reference: `${runId}.cnVu`,
    workflow_revision_hash: "a".repeat(64),
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    state_version: 1,
    state,
    current_node_id: "build",
    node_rail: [{ node_id: "build", state: state === "FAILED" ? "failed" : "succeeded", attempt: null }],
    cancellation: notCancellableBlock("between-nodes"),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: null,
    ended_at: null
  };
}

describe("the Board's true groups (#581)", () => {
  it("keeps a running run under Running", () => {
    const groups = projectBoardGroups([v3Run("STARTED", "run-a")], new Map());

    expect(groups.running.map((row) => row.run.run_id)).toEqual(["run-a"]);
    expect(groups.done).toEqual([]);
  });

  it("never shows a failed run under Running or Waiting -- it stopped, so it groups with what is over", () => {
    const groups = projectBoardGroups([v3Run("FAILED", "run-failed")], new Map());

    expect(groups.running).toEqual([]);
    expect(groups.needsYou).toEqual([]);
    expect(groups.done.map((row) => row.run.run_id)).toEqual(["run-failed"]);
    expect(groups.done[0]?.status).toEqual({ kind: "failed", nodeId: "build" });
  });

  it("never shows a cancelled run under Running or Waiting -- it stopped, so it groups with what is over", () => {
    const groups = projectBoardGroups([v3Run("CANCELLED", "run-cancelled")], new Map());

    expect(groups.running).toEqual([]);
    expect(groups.needsYou).toEqual([]);
    expect(groups.done.map((row) => row.run.run_id)).toEqual(["run-cancelled"]);
    expect(groups.done[0]?.status).toEqual({ kind: "cancelled" });
  });

  it("keeps a run waiting for a human under Needs you, not Running", () => {
    const groups = projectBoardGroups([v3Run("WAITING_INPUT", "run-waiting")], new Map());

    expect(groups.needsYou.map((row) => row.run.run_id)).toEqual(["run-waiting"]);
    expect(groups.running).toEqual([]);
  });
});
