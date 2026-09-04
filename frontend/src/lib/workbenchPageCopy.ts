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
   * The composer's own reminder while whether a conductor is even there is
   * still being read (#1103, #1114): Send is locked for the same reason a
   * lost connection locks it, so the hint says this is a passing moment, not
   * a standing refusal -- distinct from `composerHint` below, which is the
   * settled "no conductor" answer.
   */
  composerHintReading: "Checking whether a conductor is connected…",
  /**
   * The one honest sentence the ear carries while no conductor exists at all
   * (HEART, "The ear"): Send is visibly locked for this state (#1103), so the
   * sentence names that instead of promising a kept-but-unsent word.
   */
  composerHint: "No conductor is connected yet, so Send stays locked. Start work from the Catalog instead.",
  /**
   * The reply a sent message gets when the conductor's own connection read
   * itself failed ("unreadable"): the only state left where a message can
   * still reach the local-chat fallback and be answered as if no conductor
   * existed -- "reading" locks the composer instead (#1114), and "absent",
   * "unbound" and "not-startable" (#1103) each carry a real reason and lock
   * it too. "Until you reload" is the conversation's real boundary: it
   * survives in-app rail navigation (the module that owns it outlives the
   * page component) but not a reload.
   */
  conductorConnectionUnknown:
    "Nothing was started. Your message is kept in this conversation until you reload the page.",
  /**
   * The failed line's own notice, beside its Resend control (#1078 B4): the
   * composer never clears until a write is confirmed, so a send that could
   * not be confirmed stands here instead of vanishing with no error and no
   * way back.
   */
  conductorMessageFailed: "This message was not sent.",
  resendConductorMessage: "Resend",

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
  ): string => notStartableSentence(modelId, reason, probeFailedAgo),
  /**
   * The composer's own reminder for the same state, beside the disabled
   * Send -- built the same way as `emptyDescriptionNotStartable` so it names
   * the reason on its own wherever it is the only place carrying it (a
   * conversation already holds turns once "not-startable" is reached after
   * "reading"/"unreadable" resolved it). The Workbench only renders this
   * hint in that situation: while the empty room's own card is also on
   * screen it already names the same reason, and HEART's "a state is shown,
   * never restated" gives that sentence exactly one place rather than two.
   */
  composerHintNotStartable: (
    modelId: string,
    reason: ConductorNotStartableReason,
    probeFailedAgo: string | null
  ): string => notStartableSentence(modelId, reason, probeFailedAgo)
} as const;

function notStartableSentence(
  modelId: string,
  reason: ConductorNotStartableReason,
  probeFailedAgo: string | null
): string {
  return `Your conductor (${modelId}) cannot start right now: ${notStartableReasonClause(reason, probeFailedAgo)}. ${notStartableDoorClause(reason)}`;
}

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
