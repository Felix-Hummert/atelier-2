import { isRunV3, type AnyRun } from "../api/client";
import type { NodeState } from "./runProjection";
import { humanMove, runStanding, type RunStanding } from "./runState";

/**
 * The two groups the Board shows, in the order it shows them.
 *
 * The Board owns what still moves or wants a human now, never what already
 * happened -- that is History's (operator ruling #667). Only the "running"
 * and "waiting" standings ever reach a row here: the moment a run's standing
 * turns failed, cancelled or done it leaves the Board, whether that terminal
 * state arrived from a list read or was upserted straight from the attention
 * stream. There is no "done" group left to catch it.
 *
 * The mockup's fourth group, Queued, has no owner yet: no served run state
 * names a run as queued (`AnyRun["state"]` is STARTED, WAITING_INPUT,
 * WAITING_RECONCILIATION, COMPLETED, FAILED or CANCELLED). A future
 * sequencing source (#79) adds that group instead of this one inventing a
 * queue from nothing.
 *
 * `StudioPage.svelte` reads a row's group to decide its section, and its
 * `standing` to decide its mark.
 */
export const BOARD_GROUPS = ["needsYou", "running"] as const;
export type BoardGroup = (typeof BOARD_GROUPS)[number];

export type BoardRowStatus =
  | { kind: "waitingInput" }
  | { kind: "waitingReconciliation" }
  | { kind: "running"; nodeId: string };

export type MiniPipelineDot = { nodeId: string; state: NodeState };

export type BoardRow = {
  run: AnyRun;
  group: BoardGroup;
  /** The colour/shape a row's state mark reads by (`standingMarks`), independent of its group. */
  standing: RunStanding;
  name: string;
  status: BoardRowStatus;
  humanMove: string | null;
  /** Null when the run's format carries no node_rail (a V1 run). */
  miniPipeline: readonly MiniPipelineDot[] | null;
};

export type BoardGroups = Record<BoardGroup, readonly BoardRow[]>;

/**
 * Resolves a run's workflow name from the described catalog listing, keyed by
 * `workflow_revision_hash`.
 *
 * A hash the catalog never described, a described revision with no name (a V1
 * revision names nothing, per the served document), and a catalog this round
 * could not read at all (`null`) all fall back to the run id honestly --
 * never a placeholder that reads like a real name. The catalog read is
 * enrichment over the confirmed run list, not a gate on it: a run still shows
 * with its own real fields even when its name could not be resolved.
 */
export function resolveWorkflowName(
  run: AnyRun,
  workflowNames: ReadonlyMap<string, string | null> | null
): string {
  return workflowNames?.get(run.workflow_revision_hash) ?? run.run_id;
}

/**
 * The Board's live rows, grouped and ready to render.
 *
 * Filters to the "running" and "waiting" standings before building a row: a
 * run whose standing already turned failed, cancelled or done never becomes
 * a Board row, even one just upserted straight from the attention stream.
 */
export function projectBoardGroups(
  runs: readonly AnyRun[],
  workflowNames: ReadonlyMap<string, string | null> | null
): BoardGroups {
  const rows = runs
    .filter((run) => isLive(runStanding(run.state)))
    .map((run) => boardRow(run, workflowNames));
  return {
    needsYou: rows.filter((row) => row.group === "needsYou"),
    running: rows.filter((row) => row.group === "running")
  };
}

function isLive(standing: RunStanding): boolean {
  return standing === "running" || standing === "waiting";
}

function boardRow(run: AnyRun, workflowNames: ReadonlyMap<string, string | null> | null): BoardRow {
  const standing = runStanding(run.state);
  return {
    run,
    group: boardGroup(standing),
    standing,
    name: resolveWorkflowName(run, workflowNames),
    status: rowStatus(run),
    humanMove: humanMove(run.state),
    miniPipeline: miniPipeline(run)
  };
}

/** Only ever called with the "waiting" or "running" standing `projectBoardGroups` already filtered to. */
function boardGroup(standing: RunStanding): BoardGroup {
  return standing === "waiting" ? "needsYou" : "running";
}

/** Only ever called on a run `projectBoardGroups` already filtered to a live standing. */
function rowStatus(run: AnyRun): BoardRowStatus {
  if (run.state === "WAITING_INPUT") return { kind: "waitingInput" };
  if (run.state === "WAITING_RECONCILIATION") return { kind: "waitingReconciliation" };
  return { kind: "running", nodeId: currentNodeId(run) };
}

function currentNodeId(run: AnyRun): string {
  return isRunV3(run) ? run.current_node_id : run.current_node.node_id;
}

function miniPipeline(run: AnyRun): readonly MiniPipelineDot[] | null {
  if (!("node_rail" in run)) return null;
  return run.node_rail.map((entry) => ({ nodeId: entry.node_id, state: entry.state }));
}
