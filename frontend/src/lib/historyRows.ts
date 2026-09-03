import type { RunV3 } from "../api/client";
import { decodeUtf8Base64 } from "./exactBytes";
import { historyOutcome } from "./historyOutcome";
import { newestActivityFirst, runActivityAt } from "./runList";
import { trackerItemHref, trackerItemLabel, type TrackerSourceConnection } from "./trackerItem";
import { parseUtc } from "./when";

/** The window the silent period chip names by default (mockup v5 §05: "7 days"). */
export const HISTORY_PERIOD_DAYS = 7;

const DAY_MS = 24 * 60 * 60 * 1000;

export type HistoryWorkItem = {
  reference: string;
  /** Tracker enrichment title only. Null when enrichment is unavailable. */
  title: string | null;
  href: string | null;
};

export type HistoryRowResult =
  | { kind: "completed"; sentence: string | null }
  | { kind: "omitted"; sentence: "answer-too-large" }
  | { kind: "failed"; nodeId: string; sentence: string | null };

export type HistoryWhenDay =
  | { kind: "today" }
  | { kind: "yesterday" }
  | { kind: "weekday"; weekday: string };

export type HistoryWhenLabel = {
  day: HistoryWhenDay;
  clock: string;
};

export type HistoryRow = {
  run: RunV3;
  /** The purpose of a History row is the workflow the row's own run names (#1045). */
  workflowName: string;
  /**
   * The run's work item: reference plus enrichment title when a real source
   * connection can resolve one. Never derived from order names, job prose,
   * or a guessed title. Null when no work item hangs on the run.
   */
  workItem: HistoryWorkItem | null;
  result: HistoryRowResult;
  /** Only ever a real pair with both stamps present -- never guessed for a partial row. */
  span: { startedAt: string; endedAt: string } | null;
  /** The same "last known movement" stamp `runList.ts` orders by; null for a run with no timestamp. */
  activityAt: string | null;
};

/**
 * The finished runs History shows, newest activity first.
 *
 * Only COMPLETED and FAILED runs become a row: this is what "ist gelaufen"
 * (has run) means for History, unlike the Workbench, which still holds a run
 * that moves or waits. Every fact a row shows -- workflow, work item,
 * terminal result -- is read straight off the run the list already returned
 * (#1045): no second request per row and no second request per revision hash.
 */
export function projectHistoryRows(
  runs: readonly RunV3[],
  sourceConnection: TrackerSourceConnection | null
): HistoryRow[] {
  const finished = runs.filter((run) => run.state === "COMPLETED" || run.state === "FAILED");
  return newestActivityFirst(finished).map((run) => presentHistoryRow(run, sourceConnection));
}

export function presentHistoryRow(
  run: RunV3,
  sourceConnection: TrackerSourceConnection | null
): HistoryRow {
  return {
    run,
    workflowName: run.workflow_name,
    workItem: historyWorkItem(run, sourceConnection),
    result: historyResult(run),
    span: historySpan(run),
    activityAt: runActivityAt(run)
  };
}

/** Reference as a link label: grammar, plus title only when enrichment supplied one. */
export function historyWorkItemLabel(item: HistoryWorkItem): string {
  const grammar = trackerItemLabel(item.reference);
  if (item.title !== null && item.title.length > 0) return `${grammar} ${item.title}`;
  return grammar;
}

function historyWorkItem(
  run: RunV3,
  sourceConnection: TrackerSourceConnection | null
): HistoryWorkItem | null {
  const reference = run.work_item_reference;
  if (reference == null) return null;
  return { reference, title: null, href: trackerItemHref(reference, sourceConnection) };
}

function historyResult(run: RunV3): HistoryRowResult {
  // A completed run whose answer was omitted for size (#1045) is a fact this
  // row already knows without decoding anything: it never collapses into the
  // same "not recorded" a run that wrote nothing would show.
  if (run.answer?.kind === "omitted") {
    return { kind: "omitted", sentence: "answer-too-large" };
  }
  const sentence = historyResultSentence(run);
  if (run.state === "FAILED") {
    return { kind: "failed", nodeId: historyFailedNodeId(run), sentence };
  }
  return { kind: "completed", sentence };
}

function historyResultSentence(run: RunV3): string | null {
  const encoded =
    run.state === "FAILED"
      ? run.refusal_output?.value_base64
      : answerValueBase64(run.answer);
  if (encoded == null || encoded.length === 0) return null;
  const decoded = decodeUtf8Base64(encoded);
  if (decoded == null || decoded.length === 0) return null;
  return historyOutcome(run.workflow_name, decoded);
}

/**
 * The row's own accepted answer bytes, or none for a run that named none and
 * for one whose value the list omitted for size (#1045) -- honest, since a
 * derived sentence needs the actual bytes and this row was never given them.
 */
function answerValueBase64(answer: RunV3["answer"]): string | undefined {
  return answer?.kind === "value" ? answer.value_base64 : undefined;
}

function historySpan(run: RunV3): { startedAt: string; endedAt: string } | null {
  if (run.started_at == null || run.ended_at == null) return null;
  return { startedAt: run.started_at, endedAt: run.ended_at };
}

/**
 * The node a failed run failed at: the rail names it directly, with the
 * current node as the honest fallback for a rail no entry of which failed.
 */
function historyFailedNodeId(run: RunV3): string {
  const failed = run.node_rail.find((entry) => entry.state === "failed");
  return failed?.node_id ?? run.current_node_id;
}

/**
 * Local calendar-clock fragments for the When cell.
 *
 * Day membership is the local calendar day against `now`, never the UTC date
 * the ISO stamp names. Weekday text comes from the locale clock; this helper
 * does not own English day words.
 */
export function historyWhenLabel(iso: string, now: Date): HistoryWhenLabel {
  const at = parseUtc(iso);
  const clock = `${padClock(at.getHours())}:${padClock(at.getMinutes())}:${padClock(at.getSeconds())}`;
  const dayOffset = localDayOffset(at, now);
  if (dayOffset === 0) return { day: { kind: "today" }, clock };
  if (dayOffset === 1) return { day: { kind: "yesterday" }, clock };
  return {
    day: { kind: "weekday", weekday: at.toLocaleDateString(undefined, { weekday: "short" }) },
    clock
  };
}

function padClock(value: number): string {
  return String(value).padStart(2, "0");
}

function localDayOffset(at: Date, now: Date): number {
  const atDay = Date.UTC(at.getFullYear(), at.getMonth(), at.getDate());
  const nowDay = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((nowDay - atDay) / DAY_MS);
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
