/**
 * Copy the History surface renders: only finished runs, as ruhige Zeilen
 * (mockup v5 §05) -- no Start, no Refresh, no queue prose.
 *
 * One owner for this screen's strings, the same convention `workflowsPageCopy`
 * and `studioPageCopy` already hold to, so `?pseudo-locale=1`
 * (`wrapDisplayCopy`) can prove every string here has a source instead of a
 * second hardcoded copy inline in the page.
 */
export const historyPageCopy = {
  eyebrow: "Atelier",
  title: "History",
  looking: "Looking…",
  retry: "Retry",
  listUnavailable: "History unavailable",
  listIncomplete: "History incomplete",
  emptyTitle: "No finished runs yet",
  emptyDescription: "A run appears here once it completes or fails.",
  columnName: "Name",
  columnResult: "Result",
  columnDuration: "Took",
  resultCompleted: "Completed",
  resultFailedAt: "Failed at",
  durationNotRecorded: "Not recorded",
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
