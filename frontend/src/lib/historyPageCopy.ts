import { readStateCopy } from "./readStateCopy";

/**
 * Copy the History surface renders: only finished runs, as ruhige Zeilen
 * (mockup v8 §05) -- no Start, no Refresh, no queue prose.
 *
 * One owner for this screen's strings, the same convention `catalogPageCopy`
 * and `workbenchPageCopy` already hold to, so `?pseudo-locale=1`
 * (`wrapDisplayCopy`) can prove every string here has a source instead of a
 * second hardcoded copy inline in the page.
 *
 * A failed row still borrows the standing word from `standingWords` in
 * `runState.ts` so "Failed" reads the same on every surface. The completed
 * result cell is the derived half-sentence `historyOutcome.ts` maps, not the
 * standing word "Done" and not the raw result bytes.
 */

export const historyPageCopy = {
  title: "History",
  looking: readStateCopy.looking,
  retry: "Retry",
  /**
   * Completed and failed runs are read as two separate durable lists
   * (#1109 delta MEDIUM): when one stops partway, its own Retry names which
   * list it resumes, rather than two identical "Retry" buttons that both
   * happen to restart everything.
   */
  retryList: {
    completed: "Retry completed runs",
    failed: "Retry failed runs"
  },
  listUnavailable: "History unavailable",
  emptyTitle: "No finished runs yet",
  emptyDescription: "Runs land here once they finish — start one from the Catalog.",
  emptyNext: "Open the Catalog",
  /** The purpose column names the workflow. */
  columnName: "Purpose",
  columnWhen: "When",
  /** ADR 0019 §4. Honest dash when no work item hangs on the run. */
  columnWorkItem: "Work item",
  columnResult: "Result",
  columnDuration: "Duration",
  today: "today",
  yesterday: "yesterday",
  /** "—" where a run names none. */
  workItemPlaceholder: "—",
  /**
   * A V1/V2 row (or a V3 row with neither stamp) in When/Duration, and a
   * completed row with no readable result sentence.
   */
  notRecorded: "Not recorded",
  /**
   * A completed row whose own answer the list omitted for size (#1045) --
   * never `notRecorded`, which reads as a run that wrote nothing at all.
   */
  answerTooLarge: "Too large to show",
  /**
   * Only shown when a listed row carries no V3 timestamp (a V1 or V2 run):
   * names why such a row still shows under a period chip that cannot measure
   * it, rather than leaving the chip's own honesty unexplained.
   */
  timestamplessHint:
    "Runs with no recorded time always show here — the period only filters what it can measure.",
  outcome: {
    approved: "approved",
    revise: "revise",
    cannotJudge: "cannot judge",
    pass: "pass",
    buildable: "buildable",
    needsDecision: "needs a decision",
    high: "high",
    medium: "medium",
    low: "low",
    text: "text"
  }
} as const;

export function periodChipLabel(days: number): string {
  return `${days} days`;
}

/**
 * Names where a partial read stopped, beside the pages it already showed
 * (#1042 review, A2). A partial answer always beats none: this never
 * replaces the rows above it, only names the cursor the next page would
 * have started from and offers the one honest next move, Retry.
 */
export function readingStoppedAt(cursor: string): string {
  return `Reading stopped at ${cursor} — showing what already loaded.`;
}

function historyFindingsHighest(count: number, severity: string): string {
  const findings = count === 1 ? "1 finding" : `${count} findings`;
  return `${findings}, highest ${severity}`;
}

export function historyCodeReviewOutcome(
  verdict: string,
  findingCount: number,
  highest: string | null
): string {
  if (highest === null || findingCount === 0) return verdict;
  return `${verdict} · ${historyFindingsHighest(findingCount, highest)}`;
}

export function historyRefineOutcome(expectationCount: number, lensCount: number): string {
  const expectations = expectationCount === 1 ? "1 expectation" : `${expectationCount} expectations`;
  const lenses = lensCount === 1 ? "1 lens" : `${lensCount} lenses`;
  return `${expectations}, ${lenses}`;
}

export function historyBreakdownOutcome(sliceCount: number, verdict: string): string {
  const slices = sliceCount === 1 ? "1 slice" : `${sliceCount} slices`;
  return `${slices}, ${verdict}`;
}

export function historyFieldsShape(count: number): string {
  return count === 1 ? "1 field" : `${count} fields`;
}

export function historyItemsShape(count: number): string {
  return count === 1 ? "1 item" : `${count} items`;
}
