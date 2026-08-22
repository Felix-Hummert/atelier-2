import { isRunV3, type AnyRun } from "../api/client";
import { newestActivityFirst, runActivityAt } from "./runList";
import { parseUtc } from "./when";

/** The window the silent period chip names by default (mockup v5 §05: "7 days"). */
export const HISTORY_PERIOD_DAYS = 7;

const DAY_MS = 24 * 60 * 60 * 1000;

export type HistoryRowResult =
  | { kind: "completed" }
  | { kind: "failed"; nodeId: string };

export type HistoryRow = {
  run: AnyRun;
  name: string;
  result: HistoryRowResult;
  /** Only ever a real V3 pair with both stamps present -- never guessed for V1/V2 or a partial V3 row. */
  span: { startedAt: string; endedAt: string } | null;
  /** The same "last known movement" stamp `runList.ts` orders by; null for a run with no V3 timestamp. */
  activityAt: string | null;
};

/**
 * The finished runs History shows, newest activity first.
 *
 * Only COMPLETED and FAILED runs become a row: this is what "ist gelaufen"
 * (has run) means for History, unlike the Board, which still tracks a failed
 * run as live work. A run this round's catalog read could not name falls
 * back to its own run id, honestly, rather than a placeholder that reads like
 * a real name (the same choice `boardRows.ts`'s `resolveWorkflowName` makes).
 */
export function projectHistoryRows(
  runs: readonly AnyRun[],
  workflowNames: ReadonlyMap<string, string | null> | null
): HistoryRow[] {
  const finished = runs.filter((run) => run.state === "COMPLETED" || run.state === "FAILED");
  return newestActivityFirst(finished).map((run) => historyRow(run, workflowNames));
}

function historyRow(
  run: AnyRun,
  workflowNames: ReadonlyMap<string, string | null> | null
): HistoryRow {
  return {
    run,
    name: workflowNames?.get(run.workflow_revision_hash) ?? run.run_id,
    result: historyResult(run),
    span: historySpan(run),
    activityAt: runActivityAt(run)
  };
}

function historyResult(run: AnyRun): HistoryRowResult {
  if (run.state === "FAILED") return { kind: "failed", nodeId: historyFailedNodeId(run) };
  return { kind: "completed" };
}

function historySpan(run: AnyRun): { startedAt: string; endedAt: string } | null {
  if (!isRunV3(run) || run.started_at == null || run.ended_at == null) return null;
  return { startedAt: run.started_at, endedAt: run.ended_at };
}

/**
 * The node a failed run failed at.
 *
 * Mirrors `boardRows.ts`'s private `failedNodeId`/`currentNodeId`: `node_rail`
 * names it directly for a V2/V3 run; a V1 run carries no rail, so the current
 * node is read instead -- true because this engine only reaches FAILED by
 * failing the node the run's cursor was sitting on. Duplicated rather than
 * imported because History (#526) and Board (#519) sit in separate build
 * fences; `boardRows.ts` should export this once both lanes can share one.
 */
function historyFailedNodeId(run: AnyRun): string {
  if ("node_rail" in run) {
    const failed = run.node_rail.find((entry) => entry.state === "failed");
    if (failed !== undefined) return failed.node_id;
  }
  return isRunV3(run) ? run.current_node_id : run.current_node.node_id;
}

/**
 * Whether a row's real activity stamp falls in the chip's recent window.
 *
 * A row with no V3 stamp (V1/V2) is never hidden by this: the chip filters
 * only what it can honestly filter (Operator ruling 22.08.) -- it never
 * guesses a period membership it cannot measure.
 */
export function withinHistoryPeriod(
  row: HistoryRow,
  now: Date,
  days: number = HISTORY_PERIOD_DAYS
): boolean {
  if (row.activityAt === null) return true;
  const elapsedMs = now.getTime() - parseUtc(row.activityAt).getTime();
  return elapsedMs <= days * DAY_MS;
}

/** Whether any row in the set carries no V3 timestamp -- the silent hint's own gate. */
export function hasTimestamplessRows(rows: readonly HistoryRow[]): boolean {
  return rows.some((row) => row.activityAt === null);
}
