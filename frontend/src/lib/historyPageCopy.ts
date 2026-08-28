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
 * result cell is the node's own sentence, not the standing word "Done".
 */
export const historyPageCopy = {
  title: "History",
  looking: "Looking…",
  retry: "Retry",
  listUnavailable: "History unavailable",
  listIncomplete: "History incomplete",
  emptyTitle: "No finished runs yet",
  emptyDescription: "Runs land here once they finish — start one from the Catalog.",
  emptyNext: "Open the Catalog",
  /** Mockup v8 §05: "Purpose (the order sentence), workflow small beneath". */
  columnName: "Purpose",
  columnWhen: "When",
  /** ADR 0019 §4. Production rows have no projected work item yet; they read `workItemPlaceholder`. */
  columnWorkItem: "Work item",
  columnResult: "Result",
  columnDuration: "Duration",
  today: "today",
  yesterday: "yesterday",
  /** "—" where a run names none; production History always reads this. */
  workItemPlaceholder: "—",
  /**
   * A V1/V2 row (or a V3 row with neither stamp) in When/Duration, and a
   * completed row whose node extras settled with no readable sentence.
   */
  notRecorded: "Not recorded",
  /**
   * Only shown when a listed row carries no V3 timestamp (a V1 or V2 run):
   * names why such a row still shows under a period chip that cannot measure
   * it, rather than leaving the chip's own honesty unexplained.
   */
  timestamplessHint:
    "Runs with no recorded time always show here — the period only filters what it can measure."
} as const;

export function periodChipLabel(days: number): string {
  return `${days} days`;
}
