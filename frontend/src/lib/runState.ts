import type { AnyRun } from "../api/client";

/**
 * What a durable run state means for the person looking at it.
 *
 * One owner for every level of the workshop, so "waits for a human" is decided
 * once. The records below are total over the served states of *every* format a
 * listing can hold: a state added to the wire is a type error here rather than a
 * run that silently waits for nobody. `CANCELLED` is a V3-only word (#439 P1) --
 * no writer produces it before #439 P3 gives an operator's run-cancel command
 * its own end-of-run seam, but the wire vocabulary already carries it, so the
 * word is grouped here rather than left to fall through.
 */
export type RunStanding = "running" | "waiting" | "failed" | "cancelled" | "done";

const standings: Record<AnyRun["state"], RunStanding> = {
  STARTED: "running",
  WAITING_INPUT: "waiting",
  WAITING_RECONCILIATION: "waiting",
  FAILED: "failed",
  CANCELLED: "cancelled",
  COMPLETED: "done"
};

/** The move the run is waiting for from a human, in one word, or null when it waits for nobody. */
const humanMoves: Record<AnyRun["state"], string | null> = {
  STARTED: null,
  WAITING_INPUT: "Answer",
  WAITING_RECONCILIATION: "Reconcile",
  FAILED: null,
  CANCELLED: null,
  COMPLETED: null
};

/** The heading a group of runs carries where the runs are grouped. One owner, one word. */
export const standingWords: Record<RunStanding, string> = {
  running: "Running",
  waiting: "Waiting for you",
  failed: "Failed",
  cancelled: "Cancelled",
  done: "Done"
};

/** The order the standings read in: what moves, what needs you, what is behind you. */
export const standingOrder: readonly RunStanding[] = [
  "running",
  "waiting",
  "failed",
  "cancelled",
  "done"
];

/** The shape that carries the standing without colour, for eyes that read no colour. */
export const standingMarks: Record<RunStanding, string> = {
  running: "▲",
  waiting: "⬢",
  failed: "◇",
  cancelled: "⊘",
  done: "●"
};

export function humanMove(state: AnyRun["state"]): string | null {
  return humanMoves[state];
}

export function runStanding(state: AnyRun["state"]): RunStanding {
  return standings[state];
}

export function waitsForAHuman(state: AnyRun["state"]): boolean {
  return standings[state] === "waiting";
}

export function runsStanding(runs: readonly AnyRun[], standing: RunStanding): AnyRun[] {
  return runs.filter((run) => standings[run.state] === standing);
}

export function countStanding(runs: readonly AnyRun[], standing: RunStanding): number {
  return runsStanding(runs, standing).length;
}
