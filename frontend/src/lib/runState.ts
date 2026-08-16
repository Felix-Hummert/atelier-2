import type { Run } from "../api/client";

/**
 * What a durable run state means for the person looking at it.
 *
 * One owner for every level of the workshop, so "waits for a human" is decided
 * once. The records below are total over the served states: a state added to
 * the wire is a type error here rather than a run that silently waits for
 * nobody.
 */
export type RunStanding = "running" | "waiting" | "done";

const standings: Record<Run["state"], RunStanding> = {
  STARTED: "running",
  WAITING_INPUT: "waiting",
  WAITING_RECONCILIATION: "waiting",
  COMPLETED: "done"
};

/** The move the run is waiting for from a human, in one word, or null when it waits for nobody. */
const humanMoves: Record<Run["state"], string | null> = {
  STARTED: null,
  WAITING_INPUT: "Answer",
  WAITING_RECONCILIATION: "Reconcile",
  COMPLETED: null
};

/** The shape that carries the standing without colour, for eyes that read no colour. */
export const standingMarks: Record<RunStanding, string> = {
  running: "▲",
  waiting: "⬢",
  done: "●"
};

export function humanMove(state: Run["state"]): string | null {
  return humanMoves[state];
}

export function waitsForAHuman(state: Run["state"]): boolean {
  return standings[state] === "waiting";
}

export function countStanding(runs: readonly Run[], standing: RunStanding): number {
  return runs.filter((run) => standings[run.state] === standing).length;
}
