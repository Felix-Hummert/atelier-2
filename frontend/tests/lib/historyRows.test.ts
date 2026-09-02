import { describe, expect, it } from "vitest";

import type { RunV3 } from "../../src/api/client";
import { historyOutcome } from "../../src/lib/historyOutcome";
import {
  HISTORY_PERIOD_DAYS,
  hasTimestamplessRows,
  historyWhenLabel,
  historyWorkItemLabel,
  presentHistoryRow,
  projectHistoryRows,
  withinHistoryPeriod
} from "../../src/lib/historyRows";
import { notCancellableBlock } from "../support/runV3";
import { completedRun, publicReference, revisionHash } from "../support/runV3";

function failedRun(changes: Partial<RunV3> = {}): RunV3 {
  return { ...completedRun(changes), state: "FAILED" };
}

function v3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/run",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    workflow_name: "Two agents in a line",
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    work_item_reference: null,
    answer: null,
    refusal_output: null,
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "final",
    node_rail: [{ node_id: "final", state: "succeeded", attempt: null }],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: revisionHash,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: "2026-08-18T15:38:00Z",
    ...changes,
    current_node_execution_id: changes.current_node_execution_id ?? revisionHash
  };
}

function answerOf(raw: string): NonNullable<RunV3["answer"]> {
  return { kind: "value", value_base64: btoa(raw), value_hash: "f".repeat(64) };
}

function refusalOutputOf(raw: string): NonNullable<RunV3["refusal_output"]> {
  return { value_base64: btoa(raw), value_hash: "a".repeat(64) };
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

  it("names a run's workflow straight from the run itself (#1045), no second read", () => {
    const named = v3Run({ run_id: "named-run", workflow_name: "Two agents in a line" });
    const [row] = projectHistoryRows([named], null);

    expect(row?.workflowName).toBe("Two agents in a line");
  });

  it("never treats joined order names as purpose; a V3 run with orders still names only its own workflow", () => {
    const [named] = projectHistoryRows(
      [
        v3Run({
          workflow_name: "code-review",
          orders: [
            { name: "context", bytes: 12, schema_revision_hash: "d".repeat(64) },
            { name: "diff", bytes: 12, schema_revision_hash: "d".repeat(64) },
            { name: "review_questions", bytes: 6, schema_revision_hash: "d".repeat(64) }
          ]
        })
      ],
      null
    );
    expect(named?.workflowName).toBe("code-review");
  });

  it.each([
    ["a real work item reference", "gh:510", { reference: "gh:510", title: null, href: null }],
    ["no work item reference", null, null]
  ] as const)("workItem is %s", (_name, workItemReference, expected) => {
    const row = presentHistoryRow(v3Run({ work_item_reference: workItemReference }), null);
    expect(row.workItem).toEqual(expected);
  });

  it("labels a work item as the adapter grammar, plus title only when enrichment supplied one", () => {
    expect(historyWorkItemLabel({ reference: "gh:567", title: null, href: null })).toBe("#567");
    expect(
      historyWorkItemLabel({
        reference: "gh:567",
        title: "Verbinden/Lösen/Token-Türen",
        href: "https://github.com/FlexOr2/atelier-2/issues/567"
      })
    ).toBe("#567 Verbinden/Lösen/Token-Türen");
  });

  it("never derives workItem from order names", () => {
    const row = presentHistoryRow(
      v3Run({ orders: [{ name: "diff", bytes: 12, schema_revision_hash: "d".repeat(64) }] }),
      null
    );
    expect(row.workItem).toBeNull();
  });

  it("reads a completed result with a null sentence when the run carries no answer", () => {
    const [completed] = projectHistoryRows([v3Run({ answer: null })], null);
    expect(completed?.result).toEqual({ kind: "completed", sentence: null });
  });

  it("derives a completed result's sentence from the run's own answer, via the one outcome owner", () => {
    const raw = '{"answer":"PR merged"}';
    const [row] = projectHistoryRows(
      [v3Run({ workflow_name: "Two agents in a line", answer: answerOf(raw) })],
      null
    );
    expect(row?.result).toEqual({
      kind: "completed",
      sentence: historyOutcome("Two agents in a line", raw)
    });
  });

  it("reads a completed result with a null sentence when the row's answer was omitted for size (#1045)", () => {
    const [row] = projectHistoryRows(
      [
        v3Run({
          answer: { kind: "omitted", reason: "too_large", maximum_bytes: 49_152 }
        })
      ],
      null
    );
    expect(row?.result).toEqual({ kind: "completed", sentence: null });
  });

  it("reads a failed result with its node id, deriving the sentence from refusal_output", () => {
    const raw = '{"answer":"could not merge"}';
    const failed = v3Run({
      state: "FAILED",
      workflow_name: "Two agents in a line",
      node_rail: [
        { node_id: "build", state: "succeeded", attempt: null },
        { node_id: "review", state: "failed", attempt: null }
      ],
      refusal_output: refusalOutputOf(raw)
    });

    const row = presentHistoryRow(failed, null);

    expect(row.result).toEqual({
      kind: "failed",
      nodeId: "review",
      sentence: historyOutcome("Two agents in a line", raw)
    });

    const withoutRefusalOutput = presentHistoryRow({ ...failed, refusal_output: null }, null);
    expect(withoutRefusalOutput.result).toEqual({ kind: "failed", nodeId: "review", sentence: null });
  });

  it("reads the current node as the failed one when no rail entry says failed", () => {
    const [row] = projectHistoryRows([failedRun()], null);

    expect(row?.result.kind).toBe("failed");
    if (row?.result.kind === "failed") {
      expect(row.result.nodeId).toBe(failedRun().current_node_id);
      expect(row.result.sentence).toBeNull();
    }
  });

  it("gives a duration span only for a real pair of stamps, never guessed for a partial row", () => {
    const [stamplessRow] = projectHistoryRows([completedRun()], null);
    expect(stamplessRow?.span).toBeNull();
    expect(stamplessRow?.activityAt).toBeNull();

    const [partialRow] = projectHistoryRows([v3Run({ ended_at: null })], null);
    expect(partialRow?.span).toBeNull();

    const [wholeRow] = projectHistoryRows([v3Run()], null);
    expect(wholeRow?.span).toEqual({
      startedAt: "2026-08-18T15:00:00Z",
      endedAt: "2026-08-18T15:38:00Z"
    });
  });

  it("tells two same-workflow finished runs apart by their local clock", () => {
    const now = new Date("2026-08-18T16:00:00Z");
    const earlier = historyWhenLabel("2026-08-18T15:38:00Z", now);
    const later = historyWhenLabel("2026-08-18T15:38:01Z", now);
    expect(earlier.clock).not.toBe(later.clock);
  });
});

describe("historyWhenLabel names local calendar-clock fragments", () => {
  const now = new Date(2026, 7, 25, 18, 0, 0);

  it.each([
    ["today", new Date(2026, 7, 25, 9, 5, 7), { kind: "today" as const }, "09:05:07"],
    ["yesterday", new Date(2026, 7, 24, 23, 59, 1), { kind: "yesterday" as const }, "23:59:01"]
  ])("reads %s from the local calendar day", (_name, at, day, clock) => {
    expect(historyWhenLabel(at.toISOString(), now)).toEqual({ day, clock });
  });

  it("reads any other local day as that locale's short weekday", () => {
    const at = new Date(2026, 7, 20, 8, 0, 1);
    expect(historyWhenLabel(at.toISOString(), now)).toEqual({
      day: { kind: "weekday", weekday: at.toLocaleDateString(undefined, { weekday: "short" }) },
      clock: "08:00:01"
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
    const [stamplessRow] = projectHistoryRows([completedRun()], null);

    expect(stamplessRow && withinHistoryPeriod(stamplessRow, now, 0)).toBe(true);
  });

  it("defaults to a 7 day window", () => {
    expect(HISTORY_PERIOD_DAYS).toBe(7);
  });
});

describe("the timestampless hint", () => {
  it("fires only when a listed row actually carries no timestamp", () => {
    const withoutStamp = projectHistoryRows([completedRun()], null);
    const withStamp = projectHistoryRows([v3Run()], null);

    expect(hasTimestamplessRows(withoutStamp)).toBe(true);
    expect(hasTimestamplessRows(withStamp)).toBe(false);
  });
});
