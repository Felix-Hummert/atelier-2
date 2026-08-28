import { isRunV3, type AnyRun, type RunV3 } from "../api/client";
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
  runs: readonly AnyRun[],
  read: AnyRun
): AnyRun[] {
  const others = runs.filter(
    (run) => run.public_run_reference !== read.public_run_reference
  );
  return runHasEnded(read.state) ? others : [...others, read];
}

/** Open decisions the Needs-you region pins: V3 waits, one card per run. */
export function workbenchDecisionPins(runs: readonly AnyRun[]): RunV3[] {
  return runs.filter(
    (run): run is RunV3 => isRunV3(run) && run.state === "WAITING_INPUT"
  );
}
