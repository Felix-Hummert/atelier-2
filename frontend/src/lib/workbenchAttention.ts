import type { RunV3 } from "../api/client";
import { runHasEnded } from "./runState";

/**
 * The Workbench's live list of moving and waiting runs, updated from a
 * canonical `getRun` the attention stream nudged.
 *
 * Identity is the public run reference: a second delivery of the same
 * decision replaces the card rather than standing beside it. A run that has
 * ended leaves, so a decision answered elsewhere does not linger as a
 * question the run no longer asks.
 */
export function absorbAttentionRun(
  runs: readonly RunV3[],
  read: RunV3
): RunV3[] {
  const others = runs.filter(
    (run) => run.public_run_reference !== read.public_run_reference
  );
  return runHasEnded(read.state) ? others : [...others, read];
}

/** Open decisions the Needs-you region pins: open waits, one card per run. */
export function workbenchDecisionPins(runs: readonly RunV3[]): RunV3[] {
  return runs.filter((run) => run.state === "WAITING_INPUT");
}
