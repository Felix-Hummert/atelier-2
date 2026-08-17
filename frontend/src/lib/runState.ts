import type { AnyRun } from "../api/client";

/**
 * What a durable run state means for the person looking at it.
 *
 * One owner for every level of the workshop, so "waits for a human" is decided
 * once. The records below are total over the served states of *every* format a
 * listing can hold: a state added to the wire is a type error here rather than a
 * run that silently waits for nobody. A version 3 run adds no state of its own --
 * it can only be started or completed -- so grouping it needed no new word.
 */
export type RunStanding = "running" | "waiting" | "done";

const standings: Record<AnyRun["state"], RunStanding> = {
  STARTED: "running",
  WAITING_INPUT: "waiting",
  WAITING_RECONCILIATION: "waiting",
  COMPLETED: "done"
};

/** The move the run is waiting for from a human, in one word, or null when it waits for nobody. */
const humanMoves: Record<AnyRun["state"], string | null> = {
  STARTED: null,
  WAITING_INPUT: "Answer",
  WAITING_RECONCILIATION: "Reconcile",
  COMPLETED: null
};

/** The heading a group of runs carries where the runs are grouped. One owner, one word. */
export const standingWords: Record<RunStanding, string> = {
  running: "Running",
  waiting: "Waiting for you",
  done: "Done"
};

/** The order the standings read in: what moves, what needs you, what is behind you. */
export const standingOrder: readonly RunStanding[] = ["running", "waiting", "done"];

/** The shape that carries the standing without colour, for eyes that read no colour. */
export const standingMarks: Record<RunStanding, string> = {
  running: "▲",
  waiting: "⬢",
  done: "●"
};

export function humanMove(state: AnyRun["state"]): string | null {
  return humanMoves[state];
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
