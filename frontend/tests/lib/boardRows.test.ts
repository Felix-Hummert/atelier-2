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

describe("the Board's true groups (#581, #667)", () => {
  it("keeps a running run under Running", () => {
    const groups = projectBoardGroups([v3Run("STARTED", "run-a")], new Map());

    expect(groups.running.map((row) => row.run.run_id)).toEqual(["run-a"]);
    expect(groups.needsYou).toEqual([]);
  });

  it("keeps a run waiting for a human under Needs you, not Running", () => {
    const groups = projectBoardGroups([v3Run("WAITING_INPUT", "run-waiting")], new Map());

    expect(groups.needsYou.map((row) => row.run.run_id)).toEqual(["run-waiting"]);
    expect(groups.running).toEqual([]);
  });

  it.each([
    ["FAILED", "run-failed"],
    ["CANCELLED", "run-cancelled"],
    ["COMPLETED", "run-completed"]
  ] as const)(
    "never shows a %s run on the Board -- it stopped, so it belongs to History (#667)",
    (state, runId) => {
      const groups = projectBoardGroups([v3Run(state, runId)], new Map());

      expect(groups.running).toEqual([]);
      expect(groups.needsYou).toEqual([]);
    }
  );
});
