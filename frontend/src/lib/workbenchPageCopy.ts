import { PRODUCT_NAME } from "./productName";

/**
 * Copy the Workbench renders: the decisions that need the operator, the runs
 * that are moving, the conversation typed into the ear, and the one honest
 * thing the house can say while no conductor reads those words yet.
 *
 * The Workbench owns what wants you now and what is moving (ADR 0019 §1). A
 * decision is pinned until it is answered, so a request can never scroll away
 * in a growing stream (issue #580); a run that is moving lies on the living
 * shelf beneath, one click from its graph.
 *
 * Which vision or issue owns the conductor gap is repository bookkeeping, not
 * operator-facing copy (Adressaten-Regel, operator ruling 23.08.) — no issue
 * number appears in any string below.
 *
 * What a run's *state* is called is deliberately not owned here: `standingWords`
 * in `runState.ts` owns that one word, and every room reads it, so a run cannot
 * be "Done" on one surface and "Completed" on another (operator ruling 23.08.).
 */
export const workbenchPageCopy = {
  title: "Workbench",

  openTheRun: "open the run",
  answerDecision: "Answer →",
  /** HEART "Decision as stage": the warm sentence after a successful answer. */
  answerLanded: "Your answer landed.",
  pinnedDecisionsLabel: "Open decisions",

  runsIncomplete: "Workbench runs incomplete",
  /**
   * What the room says when the live hold itself fails. The word for the
   * stream's own state is not owned here: `connectionLabels` in
   * `streamStatus.ts` owns it for every surface that holds a stream.
   */
  streamUnstartable: "The live hold on this workshop could not start.",
  eventUnapplied: "What changed could not be read.",
  retryEvent: "Retry",
  runsUnavailable: "Workbench runs unavailable",
  runsLabel: "workbench runs",
  workflowNamesUnavailable: "Workflow names unavailable — showing run ids.",
  /**
   * A run whose own projection failed (#1042) reads as this quiet row, not
   * as an empty shelf and not as the whole room failing: the other runs
   * beside it read fine, and this is the one honest thing left to say about
   * the run that does not. `DefectiveRunRow.svelte` renders this row and
   * owns these three strings for every surface that lists runs, History
   * included -- one copy owner, not a second set of words for the same row
   * (operator ruling, #1042 review).
   */
  defectiveRunsLabel: "Runs that could not be read",
  defectiveRunTitle: "Could not be read",
  defectiveRunDetail: "Technical detail",

  transcriptLabel: "Conversation",
  emptyTitle: "Nothing said yet",
  /**
   * The empty room teaches the one next move (REQ-UI-24) instead of staying
   * blank: today that move is the Catalog, the one room a run is started from
   * by hand.
   */
  emptyDescription:
    "The conductor that turns what you say into runs is not built yet. Until then, start work from the Catalog.",
  emptyStart: "Open the Catalog",
  youLabel: "You",
  houseLabel: PRODUCT_NAME,
  composerRegionLabel: "Composer",
  composerLabel: "Message",
  send: "Send",
  /**
   * The one honest sentence the ear carries while no conductor is connected
   * (HEART, "The ear"): it says plainly that words are not yet turned into
   * runs, without a second button that duplicates a door.
   */
  composerHint: "No conductor is connected yet, so your words are kept here but start nothing.",
  /**
   * The reply every sent message gets while no conductor is connected. The
   * standing hint already carries "no conductor is connected yet", so the reply
   * drops that duplicated lead and keeps only its two unique truths: nothing
   * was started, and the message was not thrown away. "Until you reload" is the
   * conversation's real boundary: it survives in-app rail navigation (the
   * module that owns it outlives the page component) but not a reload.
   */
  conductorAbsent:
    "Nothing was started. Your message is kept in this conversation until you reload the page."
} as const;
