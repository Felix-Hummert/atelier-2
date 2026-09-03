import type { ConductorNotStartableReason } from "./conductorEpisode";
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

  transcriptLabel: "Conversation",
  emptyTitle: "Nothing said yet",
  /**
   * The empty room teaches the one next move (REQ-UI-24) instead of staying
   * blank: today that move is the Catalog, the one room a run is started from
   * by hand. Shown while no conductor exists at all (`absent`) or its own
   * read failed (`unreadable`) -- `emptyDescriptionUnbound` and
   * `emptyDescriptionNotStartable` below carry the two states that instead
   * name a real, existing conductor's own reason (#1103).
   */
  emptyDescription: "No conductor is connected yet. Until then, start work from the Catalog.",
  emptyStart: "Open the Catalog",
  openSettings: "Open Settings",
  youLabel: "You",
  houseLabel: PRODUCT_NAME,
  composerRegionLabel: "Composer",
  composerLabel: "Message",
  send: "Send",
  /**
   * The one honest sentence the ear carries while no conductor exists at all,
   * or its own read failed (HEART, "The ear"): it says plainly that words are
   * not yet turned into runs, without a second button that duplicates a door.
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
    "Nothing was started. Your message is kept in this conversation until you reload the page.",

  /**
   * A published conductor exists but its role carries no agent-configuration
   * binding (#1103): the empty room's own explanation, naming the role and
   * the one door that fixes it.
   */
  emptyDescriptionUnbound: (role: string): string =>
    `The conductor's "${role}" role has no agent configuration bound yet. Bind one in Settings, then start work from the Catalog until it is.`,
  /** The composer's own short reminder for the same state, beside the disabled Send. */
  composerHintUnbound: (role: string): string =>
    `The "${role}" role has no agent configuration bound yet. Settings can bind one.`,

  /**
   * A published, bound conductor configuration cannot start right now
   * (#1103): the empty room's own explanation, naming the model, the real
   * reason the server gave, and the one door that helps -- Settings for a
   * binding or model problem; for a probe problem, no retry is reachable
   * from here, so the next canary run or a Settings change is named instead.
   * `probeFailedAgo` is the caller's own relative rendering of
   * `providerProbeObservedAt` (`when.ts` owns that formatting), carried only
   * for `provider-probe-failed`.
   */
  emptyDescriptionNotStartable: (
    modelId: string,
    reason: ConductorNotStartableReason,
    probeFailedAgo: string | null
  ): string =>
    `Your conductor (${modelId}) cannot start right now: ${notStartableReasonClause(reason, probeFailedAgo)}. ${notStartableDoorClause(reason)}`,
  /** The composer's own short reminder for the same state, beside the disabled Send. */
  composerHintNotStartable: (reason: ConductorNotStartableReason): string =>
    notStartableDoorClause(reason)
} as const;

function notStartableReasonClause(
  reason: ConductorNotStartableReason,
  probeFailedAgo: string | null
): string {
  switch (reason) {
    case "agent-executor-binding-unavailable":
      return "its executor is not registered on this host";
    case "model-not-registered":
      return "its model is not registered for this provider";
    case "provider-probe-failed":
      return probeFailedAgo === null
        ? "its last provider probe failed"
        : `its last provider probe failed ${probeFailedAgo}`;
    case "provider-probe-receipt-missing":
      return "no provider probe has proven it yet";
  }
}

function notStartableDoorClause(reason: ConductorNotStartableReason): string {
  switch (reason) {
    case "agent-executor-binding-unavailable":
    case "model-not-registered":
      return "Settings can fix this.";
    case "provider-probe-failed":
    case "provider-probe-receipt-missing":
      return "The next canary run or a Settings change re-arms it.";
  }
}
