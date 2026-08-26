import { describe, expect, it } from "vitest";

import type { RunV1, RunV3 } from "../../src/api/client";
import {
  HISTORY_PERIOD_DAYS,
  hasTimestamplessRows,
  projectHistoryRows,
  withinHistoryPeriod
} from "../../src/lib/historyRows";
import { notCancellableBlock } from "../support/runV3";
import { completedRun, publicReference, revisionHash } from "../support/workflowV1";

function v1Failed(changes: Partial<RunV1> = {}): RunV1 {
  return { ...completedRun(changes), state: "FAILED" };
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
    orders: [],
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "final",
    node_rail: [{ node_id: "final", state: "succeeded", attempt: null }],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: revisionHash,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: "2026-08-18T15:38:00Z",
    ...changes
  };
}

describe("projecting History's finished-run rows", () => {
  it("keeps only completed and failed runs, dropping anything still tracked live", () => {
    const rows = projectHistoryRows(
      [
        v3Run({ public_run_reference: "run1.YQ", run_id: "done" }),
        v3Run({ public_run_reference: "run1.Yg", run_id: "still-going", state: "STARTED" }),
        v3Run({ public_run_reference: "run1.Yw", run_id: "waiting", state: "WAITING_INPUT" })
      ],
      null
    );

    expect(rows.map((row) => row.run.run_id)).toEqual(["done"]);
  });

  it("orders newest activity first, the same owner runList.ts already uses", () => {
    const rows = projectHistoryRows(
      [
        v3Run({ public_run_reference: "run1.b2xk", run_id: "older", ended_at: "2026-08-18T10:00:00Z" }),
        v3Run({ public_run_reference: "run1.bmV3", run_id: "newer", ended_at: "2026-08-18T16:00:00Z" })
      ],
      null
    );

    expect(rows.map((row) => row.run.run_id)).toEqual(["newer", "older"]);
  });

  it("names a run's workflow from the resolved catalog, falling back to its run id honestly", () => {
    const named = v3Run({ run_id: "named-run" });
    const rows = projectHistoryRows(
      [named],
      new Map([[revisionHash, "Two agents in a line"]])
    );

    expect(rows[0]?.workflowName).toBe("Two agents in a line");

    const unresolved = projectHistoryRows([named], new Map());
    expect(unresolved[0]?.workflowName).toBe("named-run");
  });

  it("names a V3 run's purpose from its own orders, joined, without reading a node or parsing a job", () => {
    const [row] = projectHistoryRows(
      [v3Run({ orders: [{ name: "diff", bytes: 12, schema_revision_hash: "d".repeat(64) }] })],
      null
    );

    expect(row?.purpose).toBe("diff");

    const [multiOrderRow] = projectHistoryRows(
      [
        v3Run({
          orders: [
            { name: "diff", bytes: 12, schema_revision_hash: "d".repeat(64) },
            { name: "target_file", bytes: 6, schema_revision_hash: "d".repeat(64) }
          ]
        })
      ],
      null
    );
    expect(multiOrderRow?.purpose).toBe("diff, target_file");
  });

  it("names no purpose for a run started with no orders, or a V1 run that carries none at all", () => {
    const [v3NoOrders] = projectHistoryRows([v3Run({ orders: [] })], null);
    expect(v3NoOrders?.purpose).toBeNull();

    const [v1Row] = projectHistoryRows([completedRun()], null);
    expect(v1Row?.purpose).toBeNull();
  });

  it("reads Completed for a completed run and the failed node for a failed one", () => {
    const [completed] = projectHistoryRows([v3Run()], null);
    expect(completed?.result).toEqual({ kind: "completed" });

    const failed = v3Run({
      state: "FAILED",
      node_rail: [
        { node_id: "build", state: "succeeded", attempt: null },
        { node_id: "review", state: "failed", attempt: null }
      ]
    });
    const [failedRow] = projectHistoryRows([failed], null);
    expect(failedRow?.result).toEqual({ kind: "failed", nodeId: "review" });
  });

  it("reads the current node as the failed one for a V1 run, which carries no rail", () => {
    const [row] = projectHistoryRows([v1Failed()], null);

    expect(row?.result.kind).toBe("failed");
    if (row?.result.kind === "failed") {
      expect(row.result.nodeId).toBe(v1Failed().current_node.node_id);
    }
  });

  it("gives a duration span only for a real V3 pair, never guessed for V1 or a partial V3 row", () => {
    const [v1Row] = projectHistoryRows([completedRun()], null);
    expect(v1Row?.span).toBeNull();
    expect(v1Row?.activityAt).toBeNull();

    const [partialRow] = projectHistoryRows([v3Run({ ended_at: null })], null);
    expect(partialRow?.span).toBeNull();

    const [wholeRow] = projectHistoryRows([v3Run()], null);
    expect(wholeRow?.span).toEqual({
      startedAt: "2026-08-18T15:00:00Z",
      endedAt: "2026-08-18T15:38:00Z"
    });
  });
});

describe("the silent period chip filters only what it can honestly measure", () => {
  const now = new Date("2026-08-25T00:00:00Z");

  it("keeps a V3 row inside the window and drops one that fell out of it", () => {
    const [rowInside] = projectHistoryRows(
      [v3Run({ ended_at: "2026-08-20T00:00:00Z" })],
      null
    );
    const [rowOutside] = projectHistoryRows(
      [v3Run({ ended_at: "2026-08-10T00:00:00Z" })],
      null
    );

    expect(rowInside && withinHistoryPeriod(rowInside, now)).toBe(true);
    expect(rowOutside && withinHistoryPeriod(rowOutside, now)).toBe(false);
  });

  it("never hides a run with no recorded time, regardless of how old the window is", () => {
    const [v1Row] = projectHistoryRows([completedRun()], null);

    expect(v1Row && withinHistoryPeriod(v1Row, now, 0)).toBe(true);
  });

  it("defaults to a 7 day window", () => {
    expect(HISTORY_PERIOD_DAYS).toBe(7);
  });
});

describe("the timestampless hint", () => {
  it("fires only when a listed row actually carries no V3 timestamp", () => {
    const withoutStamp = projectHistoryRows([completedRun()], null);
    const withStamp = projectHistoryRows([v3Run()], null);

    expect(hasTimestamplessRows(withoutStamp)).toBe(true);
    expect(hasTimestamplessRows(withStamp)).toBe(false);
  });
});
