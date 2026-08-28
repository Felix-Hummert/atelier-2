import type { RunNotCancellableReason, RunStateV3 } from "../api/client";
import type { NodeState } from "./runProjection";
import type { ForkPlan } from "./runFork";

/**
 * Words the V3 run page speaks. One owner, the #333 ruling: Prompt / Output / Log.
 *
 * The Log tab is the stored attempt transcript once an attempt has ended.
 * A working node has not written that transcript yet, so `processLogInLease`
 * still names the live process log that stays in the lease (#104 / #9).
 * Usage and a provider-resolved model are not recorded receipt fields, so
 * their empty words live here rather than being invented at the call site.
 * The model on the receipt is the configuration's declared model. "Yet" is
 * only for a node that may still write; a finished node is not waiting for
 * those facts.
 *
 * The page reads in one order (operator ruling 23.08.): what this run is and
 * where it stands, what needs the operator now, the run as a picture, and
 * everything else only behind a click. Every fact below that is neither the
 * run's identity nor the pending decision lives inside the node panel's tabs.
 */

const looking = "Looking…";
const retry = "Retry";
const discard = "Discard";

export const runPageCopy = {
  prompt: "Prompt",
  output: "Output",
  who: "Who",
  started: "started",
  ended: "ended",
  duration: "duration",
  attempt: "attempt",
  usage: "Usage",
  declaredModel: "Declared model",
  resolvedModel: "Resolved model",
  notRecorded: "not recorded yet",
  notRecordedEnded: "not recorded",
  usageMissingWhy: "Why usage is missing",
  usageMissingExact:
    "No receipt records usage, so this panel cannot say what the attempt cost. Time is recorded beside the attempt, not on the receipt.",
  resolvedModelMissingWhy: "Why resolved model is missing",
  resolvedModelMissingExact:
    "No receipt records a provider-resolved model, so this panel cannot say which model the provider actually ran.",
  promptEmpty: "Not composed yet.",
  promptEmptyEnded: "Not composed.",
  outputEmpty: "Nothing written yet.",
  outputEmptyEnded: "Nothing written.",
  waitAnswerNotReadable:
    "You answered this gate. The answer itself is not yet kept readable after completion.",
  savedAnswerUnreadable: "The saved exact answer could not be read.",
  answerUnconfirmed: "The answer could not be confirmed.",
  exactRetryUnconfirmed: "The exact retry could not be confirmed.",
  enterAnswer: "Enter an answer.",
  whoEmpty: "No receipt yet.",
  whoEmptyEnded: "No receipt.",
  processLogInLease: "Process log stays in the lease.",
  needsYou: "Needs you",
  questionMissing: "This step is waiting for you, but it carries no question.",
  questionLooking: looking,
  answerLabel: "Your answer",
  answerSubmit: "Answer",
  answerYes: "Yes",
  answerNo: "No",
  answeredPrefix: "Answered:",
  answerContext: "What this decision reads",
  answerContextNone: "This step reads no earlier result.",
  answerContextLooking: "Reading what came before…",
  answerContextUnreadable: "This result could not be read.",
  tabResult: "Result",
  tabInput: "Input",
  tabPrompt: "Prompt",
  tabLog: "Log",
  tabEvidence: "Evidence",
  tabsLabel: "What this node carries",
  assistantTurn: "Assistant turn",
  doorCall: "Door call",
  doorAnswer: "Door answer",
  attemptStdout: "Attempt stdout",
  argumentsFold: "arguments",
  transcriptEmpty: "no transcript for this attempt",
  redacted: "redacted",
  transcriptRegion: "Stored attempt transcript",
  usageInput: "input",
  usageOutputTokens: "output tokens",
  logAbsent:
    "No process log is kept. It stays inside the executor's lease while the node runs and nothing stores it afterwards.",
  inputReads: "Reads from",
  inputNone: "This node reads no earlier node.",
  inputElsewhere: "What each of those wrote stands on its own node, under Result.",
  evidenceRun: "This run",
  evidenceRunIntro: "Every fingerprint below seals exactly what it names.",
  evidenceGap:
    "A full evidence dossier — the receipt chain, cost, and a verifiable export — is not built. What the receipt records today stands above.",
  streamStale: "This page is not following the run right now.",
  readAgain: "Read again",
  eventEvidence: "Event evidence",
  terminalHash: "Terminal hash",
  runConfiguration: "Run configuration",
  workflowRevision: "Workflow revision",
  promptHash: "Prompt hash",
  outputHash: "Output hash",
  receiptHash: "Receipt hash",
  refusedOutput: "What the model wrote",
  refusedOutputHash: "Refused output hash",
  refusedOutputRedactionNotice:
    "Credential-shaped text is replaced before this is shown. The hash on the Evidence tab still proves the exact bytes a schema judged.",
  sealsPrompt: "exactly these prompt bytes, not the receipt request hash that frames identity around them",
  sealsOutput: "exactly these output bytes",
  sealsReceipt: "the receipt that named who ran this node",
  sealsRefusedOutput: "exactly the bytes the schema refused",
  sealsWorkflow: "the exact document this run followed",
  sealsConfiguration: "the agents and inputs this run started under",
  sealsTerminal: "the finished result, so a later reader can prove it was not altered",
  sealsEvent: "this durable event",
  runUnavailable: "Run unavailable",
  runUnloadable: "The durable run could not be loaded.",
  differentDurableRun: "The API returned a different durable run.",
  workflowRevisionMismatch: "The workflow revision did not match the durable run.",
  eventStreamUnstartable: "The durable event stream could not start.",
  eventUnverified: "The durable event could not be verified.",
  eventCouldNotReconcileAnswer: "The durable event could not reconcile the saved exact answer.",
  savedRequestWrongOperation: "The saved request identity belongs to another operation.",
  savedAnswerWrongNode: "The saved exact answer does not belong to this waiting node.",
  multipleReconciliationsSaved: "More than one exact reconciliation is saved for this node.",
  savedDecisionWrongReconciliation: "The saved exact decision does not belong to this reconciliation.",
  pendingCommandDiffersFromDecision: "The pending durable command differs from the saved exact decision.",
  exactRequestWrongKind: "The exact request has the wrong kind.",
  reconciliationResponseUnproven: "The reconciliation response did not prove the exact request.",
  pendingDecisionTreatedComplete: "A pending decision was incorrectly treated as durable completion.",
  boundWorkflowUnavailable: "The bound workflow revision is unavailable.",
  acceptedRequestChangedKind: "The accepted request changed kind.",
  acceptedDecisionUnbound: "The accepted decision did not remain bound to its reconciliation.",
  acceptedCommandDiffers: "The accepted durable command differs from the exact request.",
  answerResponseUnproven: "The answer response did not prove the exact request.",
  pendingAnswerTreatedComplete: "A pending answer was incorrectly treated as durable completion.",
  answerResponseWrongFormat: "The answer response was not this run's format.",
  refreshing: "Refreshing",
  retry,
  discard,
  eventInvalid: "Event invalid",
  state: "State",
  events: "Events",
  noDurableEvents: "No durable events yet.",
  looking,
  workflowUnavailable: "Workflow unavailable",
  whereThisRunStands: "Where this run stands",
  waitQuestionUnreadable: "The wait question could not be read",
  graphUnreadable: "The graph could not be read",
  workflowGraphUnreadable: "The workflow graph could not be read.",
  documentMismatch: "The document the workshop returned is not the one this run followed.",
  olderDocumentFormat: "This run follows an older document format this page cannot draw.",
  nodeUnreadable: "This node could not be read",
  waitNode: "Wait node",
  answerNeeded: "Answer needed",
  integerAnswer: "Integer answer",
  canonicalInteger: "Use one canonical integer.",
  closeNodeDetail: "Close node detail",
  stoppedHere: "Stopped here:",
  waitingForWork: "Waiting for the work before it. Nothing has been refused.",
  workflow: "Workflow",
  workItem: "Work item",
  bindingMissing: "Binding missing",
  apiKey: "API key",
  subscription: "Subscription",
  attemptHeading: "Attempt",
  bytes: "bytes",
  verifiedOutput: "Verified output",
  verified: "✓ Verified",
  format: "Format",
  utf8: "UTF-8",
  binary: "Binary",
  empty: "Empty",
  fingerprint: "Fingerprint",
  outputFingerprint: "Output fingerprint",
  details: "Details",
  latestEvent: "Latest event",
  none: "None",
  request: "Request",
  reconciliation: {
    title: "Reconciliation",
    sending: "Sending decision",
    pending: "Decision pending",
    uncertain: "Decision uncertain",
    actor: "Actor",
    evidence: "Evidence",
    command: "Command",
    effectId: "Effect ID",
    result: "Result",
    decision: "Decision",
    authoritativeAbsence: "Authoritative absence",
    decisionNeeded: "Decision needed",
    effect: "Effect",
    hash: "Hash",
    version: "Version",
    requestInfo: "Request info",
    found: "Found",
    absent: "Absent",
    exactResult: "Exact result (base64)",
    resolve: "Resolve",
    review: "Review",
    executeQuestion: "Execute this exact effect?",
    executeOnce: (productName: string) => `${productName} will execute the exact request once.`,
    cancel: "Cancel",
    execute: "Execute",
    nameActor: "Name the accountable actor.",
    recordEvidence: "Record the evidence inspected.",
    nameEffectId: "Name the exact effect ID.",
    canonicalResult: "Use canonical standard base64 for the exact result.",
    invalidResult: "Invalid result",
    emptyResult: "Empty result",
    unconfirmed: "The decision could not be confirmed."
  },
  cancel: {
    /** The label above the control, in state hue only where it asks something. */
    eyebrow: "Stop this run",
    /** The button that opens the staged decision — never fires the cancel itself. */
    open: "Cancel this run",
    /** The staged decision (HEART "Decision as stage"): a real question, honest buttons. */
    question: "Cancel this run?",
    /** What cancelling costs, in the two shapes a cancellable run can have. */
    consequenceWorking:
      "The agent working now is stopped and the run ends here. This cannot be undone.",
    consequenceWaiting:
      "This run is waiting for your answer. Cancelling ends it here instead, unanswered. This cannot be undone.",
    confirm: "Cancel run",
    dismiss: "Keep running",
    /** In flight, in the operator's words. */
    sending: "Stopping this run",
    accepted: "Stopping this run",
    acceptedNote: "The run will end once the agent lets go.",
    /** A network failure or unconfirmed reply keeps the exact command for these. */
    uncertain: "Cancel uncertain",
    unconfirmed: "The cancel could not be confirmed.",
    retry,
    discard
  },
  fork: {
    retryHere: "Retry here",
    fromNode: (nodeId: string) => `from ${nodeId}`,
    confirmTitle: (nodeId: string) => `Retry from ${nodeId}?`,
    sheetLabel: "Confirm retry from this node",
    carriedOver: "Carried over",
    runsAgain: "Runs again",
    origin: "Origin",
    startAgain: "Start again",
    back: "Back",
    again: "again",
    successorLineage: (name: string, nodeId: string) => `Fork of ${name} from ${nodeId}`,
    originSuccessor: (name: string, nodeId: string) => `${name} from ${nodeId}`,
    unconfirmed: "The retry could not be confirmed.",
    /** Why the door is absent when a node panel is open and the plan is not ok (#105). */
    unavailableRunning: "This run is still going. Retry here waits until it has ended.",
    unavailableUnknownNode: "This step is not on this run's line.",
    unavailablePrefix:
      "A step before this one did not succeed, so this run cannot be started again from here.",
    /** The ruled deferral: this door does not change any size (#105 line 2). */
    deferralSentence:
      "Starting again from here changes no size: the model, the workflow, and the check budget stay as they were."
  }
} as const;

/**
 * What the staged cancel decision says it will cost, for the run in front of it.
 *
 * A run resting at a pause has no agent working, so promising to stop one would
 * be a sentence the operator can see is false -- and the real cost, an answer
 * nobody will now give, would go unsaid at the one moment it decides the answer.
 * Every other cancellable standing is a live attempt, which is the other line.
 */
export function cancelConsequence(state: RunStateV3): string {
  return state === "WAITING_INPUT"
    ? runPageCopy.cancel.consequenceWaiting
    : runPageCopy.cancel.consequenceWorking;
}

/**
 * The sentence the cockpit shows when the server says a run cannot be cancelled
 * right now -- #439 D3's closed reason set in the operator's words, never a raw
 * token and never a grey disabled button. The server owns *whether*; this owns
 * *how it reads*.
 */
export function cancelReasonSentence(
  reason: RunNotCancellableReason,
  currentNodeId: string
): string {
  switch (reason) {
    case "between-nodes":
      return "No agent is running that this cancel could stop. When the next agent starts, the cancel returns here.";
    case "waiting-for-you":
      return "This run is waiting for your answer, not running. Answer it or leave it as it stands.";
    case "node-runs-no-agent":
      return `${currentNodeId} runs no agent, so there is nothing here to stop.`;
    case "already-cancelling":
      return "This run is already being cancelled.";
    case "already-ended":
      return "This run has already ended.";
    case "answer-in-flight":
      return "Your answer to this run is still being applied. Once it has landed, the cancel returns here.";
  }
}

/**
 * The sentence the cockpit shows when a node panel is open but retry-from-node
 * is not available -- the same visible-sentence shape as `cancelReasonSentence`,
 * never a grey disabled button. `planRunFork` owns *whether*; this owns *how it
 * reads* (#105).
 */
export function forkUnavailableSentence(plan: ForkPlan): string | null {
  switch (plan.kind) {
    case "running":
      return runPageCopy.fork.unavailableRunning;
    case "unknown-node":
      return runPageCopy.fork.unavailableUnknownNode;
    case "prefix-not-reusable":
      return runPageCopy.fork.unavailablePrefix;
    case "ok":
      return null;
  }
}

const ENDED_NODE_STATES: ReadonlySet<NodeState> = new Set([
  "failed",
  "succeeded",
  "cancelled",
  "interrupted"
]);

export function nodeHasEnded(state: NodeState): boolean {
  return ENDED_NODE_STATES.has(state);
}

export function emptyPromptCopy(state: NodeState): string {
  return nodeHasEnded(state) ? runPageCopy.promptEmptyEnded : runPageCopy.promptEmpty;
}

export function emptyOutputCopy(state: NodeState): string {
  return nodeHasEnded(state) ? runPageCopy.outputEmptyEnded : runPageCopy.outputEmpty;
}

export function emptyWhoCopy(state: NodeState): string {
  return nodeHasEnded(state) ? runPageCopy.whoEmptyEnded : runPageCopy.whoEmpty;
}

export function notRecordedCopy(state: NodeState): string {
  return nodeHasEnded(state) ? runPageCopy.notRecordedEnded : runPageCopy.notRecorded;
}

/** Grouping for token counts on a usage line. The page's copy is English. */
export function formatTokenCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

/**
 * The usage receipt line: input and output counts, and duration only when
 * this node actually closed with both timestamps.
 */
export function usageLine(
  inputTokens: number,
  outputTokens: number,
  durationWords: string | null
): string {
  const counts = `${formatTokenCount(inputTokens)} ${runPageCopy.usageInput} · ${formatTokenCount(outputTokens)} ${runPageCopy.usageOutputTokens}`;
  return durationWords === null ? counts : `${counts} · ${durationWords}`;
}

/** Names how many events the stored transcript had to drop. */
export function transcriptDroppedCopy(droppedEvents: number): string {
  return droppedEvents === 1
    ? "1 event dropped from this transcript."
    : `${droppedEvents} events dropped from this transcript.`;
}

export function byteCountCopy(bytes: number): string {
  return `${bytes} ${runPageCopy.bytes}`;
}

export function fingerprintLabel(label: string): string {
  return `${label} fingerprint`;
}

export function infoLabel(label: string): string {
  return `${label} info`;
}

export function sealsTheseBytes(label: string): string {
  return `exactly these ${label.toLowerCase()} bytes`;
}
