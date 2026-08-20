import type { NodeState } from "./runProjection";

/**
 * Words the V3 run page speaks. One owner, the #333 ruling: Prompt / Output / Log.
 *
 * Log is not on the event stream (#104). A STARTED run says that in
 * `processLogInLease` rather than inventing a progress bar. Usage and a
 * provider-resolved model are not recorded receipt fields, so their empty
 * words live here rather than being invented at the call site. The model on
 * the receipt is the configuration's declared model. "Yet" is only for a
 * node that may still write; a finished node is not waiting for those facts.
 */

export const runPageCopy = {
  prompt: "Prompt",
  output: "Output",
  who: "Who",
  duration: "Duration",
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
  whoEmpty: "No receipt yet.",
  whoEmptyEnded: "No receipt.",
  now: "Now",
  noEventsYet: "No events yet.",
  processLogInLease: "Process log stays in the lease.",
  connecting: "Connecting…",
  reconnecting: "Reconnecting",
  followingLive: "Following live",
  streamEnded: "Ended",
  streamDisconnected: "Disconnected",
  streamStopped: "Stopped",
  finished: "What finished",
  eventEvidence: "Event evidence",
  terminalHash: "Terminal hash",
  runConfiguration: "Run configuration",
  workflowRevision: "Workflow revision",
  terminalPending: "not yet",
  promptHash: "Prompt hash",
  outputHash: "Output hash",
  receiptHash: "Receipt hash",
  sealsPrompt: "exactly these prompt bytes, not the receipt request hash that frames identity around them",
  sealsOutput: "exactly these output bytes",
  sealsReceipt: "the receipt that named who ran this node",
  sealsWorkflow: "the published document this run ran against",
  sealsConfiguration: "the bindings and inputs this run started under",
  sealsTerminal: "the finished run so an export can verify it",
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
