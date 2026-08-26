/**
 * Copy the History surface renders: only finished runs, as ruhige Zeilen
 * (mockup v8 §05) -- no Start, no Refresh, no queue prose.
 *
 * One owner for this screen's strings, the same convention `catalogPageCopy`
 * and `workbenchPageCopy` already hold to, so `?pseudo-locale=1`
 * (`wrapDisplayCopy`) can prove every string here has a source instead of a
 * second hardcoded copy inline in the page.
 *
 * The word for a run's state is not owned here: `standingWords` in
 * `runState.ts` owns it for every surface, so "Done" reads the same on the
 * Workbench, on the run and in this table (operator ruling 23.08.).
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
  /** ADR 0019 §4; PR #766 projects the first work item onto a run -- until then every row reads `workItemPlaceholder`. */
  columnWorkItem: "Work item",
  columnResult: "Result",
  columnDuration: "Duration",
  /** "—" where a run names none (mockup v8 §05); also every row's honest answer until PR #766 lands a work item to show. */
  workItemPlaceholder: "—",
  /** Only a V1/V2 row, or a V3 row with neither stamp, ever reaches this -- the same gap in both When and Duration. */
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
