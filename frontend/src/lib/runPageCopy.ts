import type { NodeState } from "./runProjection";

/**
 * Words the V3 run page speaks. One owner, the #333 ruling: Prompt / Output / Log.
 *
 * Log is not on the event stream (#104). The Log tab says that in
 * `processLogInLease` rather than inventing a progress bar. Usage and a
 * provider-resolved model are not recorded receipt fields, so their empty
 * words live here rather than being invented at the call site. The model on
 * the receipt is the configuration's declared model. "Yet" is only for a
 * node that may still write; a finished node is not waiting for those facts.
 *
 * The page reads in one order (operator ruling 23.08.): what this run is and
 * where it stands, what needs the operator now, the run as a picture, and
 * everything else only behind a click. Every fact below that is neither the
 * run's identity nor the pending decision lives inside the node panel's tabs.
 */

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
    "You answered this gate. The answer itself is not yet kept readable after completion — #511 owns that.",
  waitAnswerNotReadableSource: "#511",
  whoEmpty: "No receipt yet.",
  whoEmptyEnded: "No receipt.",
  processLogInLease: "Process log stays in the lease.",
  needsYou: "Needs you",
  questionMissing: "This step is waiting for you, but it carries no question.",
  questionLooking: "Looking…",
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
  logAbsent:
    "No process log is kept. It stays inside the executor's lease while the node runs and nothing stores it afterwards. #104 owns the export that will.",
  inputReads: "Reads from",
  inputNone: "This node reads no earlier node.",
  inputElsewhere: "What each of those wrote stands on its own node, under Result.",
  evidenceRun: "This run",
  evidenceGap:
    "A full evidence dossier — the receipt chain, cost, and a verifiable export — is #511. What the receipt records today stands above.",
  streamStale: "This page is not following the run right now.",
  readAgain: "Read again",
  eventEvidence: "Event evidence",
  terminalHash: "Terminal hash",
  runConfiguration: "Run configuration",
  workflowRevision: "Workflow revision",
  promptHash: "Prompt hash",
  outputHash: "Output hash",
  receiptHash: "Receipt hash",
  sealsPrompt: "exactly these prompt bytes, not the receipt request hash that frames identity around them",
  sealsOutput: "exactly these output bytes",
  sealsReceipt: "the receipt that named who ran this node",
  sealsWorkflow: "the exact document this run followed",
  sealsConfiguration: "the agents and inputs this run started under",
  sealsTerminal: "the finished result, so a later reader can prove it was not altered",
  sealsEvent: "this durable event"
} as const;

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
