/**
 * Words the V3 run page speaks. One owner, the #333 ruling: Prompt / Output / Log.
 *
 * Log is not here because the wire does not carry it (#104). Usage is not a
 * recorded receipt field, so its empty words live here rather than being
 * invented at the call site.
 */
export const runPageCopy = {
  prompt: "Prompt",
  output: "Output",
  who: "Who",
  duration: "Duration",
  usage: "Usage",
  usageMissing: "not recorded yet",
  promptEmpty: "Not composed yet.",
  outputEmpty: "Nothing written yet.",
  whoEmpty: "No receipt yet.",
  finished: "What finished",
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
  sealsTerminal: "the finished run so an export can verify it"
} as const;
