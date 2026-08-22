import { isRunV3, type AnyRun } from "../api/client";
import { newestActivityFirst } from "./runList";
import type { NodeState } from "./runProjection";
import { humanMove, runStanding, type RunStanding } from "./runState";

/**
 * The three groups the Board shows, in the order it shows them.
 *
 * The mockup's fourth group, Queued, has no owner yet: no served run state
 * names a run as queued (`AnyRun["state"]` is STARTED, WAITING_INPUT,
 * WAITING_RECONCILIATION, COMPLETED or FAILED). A future sequencing source
 * (#79) adds that group instead of this one inventing a queue from nothing.
 *
 * A failed run groups under Running, not Done: it is still the thing the
 * operator is tracking, not a landed result. `StudioPage.svelte` reads a
 * row's group to decide its section, and its `standing` to decide its mark.
 */
export const BOARD_GROUPS = ["needsYou", "running", "done"] as const;
export type BoardGroup = (typeof BOARD_GROUPS)[number];

export type BoardRowStatus =
  | { kind: "waitingInput" }
  | { kind: "waitingReconciliation" }
  | { kind: "running"; nodeId: string }
  | { kind: "failed"; nodeId: string }
  | { kind: "completed" };

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
  /** Only ever set from a real V3 timestamp -- never guessed for V1/V2. */
  endedAt: string | null;
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

export function projectBoardGroups(
  runs: readonly AnyRun[],
  workflowNames: ReadonlyMap<string, string | null> | null
): BoardGroups {
  const rows = runs.map((run) => boardRow(run, workflowNames));
  const rowByReference = new Map(rows.map((row) => [row.run.public_run_reference, row]));
  const done = rows.filter((row) => row.group === "done");
  return {
    needsYou: rows.filter((row) => row.group === "needsYou"),
    running: rows
      .filter((row) => row.group === "running")
      .sort((a, b) => runningRank(a) - runningRank(b)),
    // Reuses the one newest-first owner (runList.ts) rather than a second
    // implementation; a Done row with no activity stamp sorts to the end.
    done: newestActivityFirst(done.map((row) => row.run)).map(
      (run) => rowByReference.get(run.public_run_reference)!
    )
  };
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
    miniPipeline: miniPipeline(run),
    endedAt: isRunV3(run) ? (run.ended_at ?? null) : null
  };
}

function boardGroup(standing: RunStanding): BoardGroup {
  if (standing === "waiting") return "needsYou";
  if (standing === "done") return "done";
  return "running";
}

/** A running row reads before a failed one within Running, as the mockup orders them. */
function runningRank(row: BoardRow): number {
  return row.status.kind === "failed" ? 1 : 0;
}

function rowStatus(run: AnyRun): BoardRowStatus {
  if (run.state === "WAITING_INPUT") return { kind: "waitingInput" };
  if (run.state === "WAITING_RECONCILIATION") return { kind: "waitingReconciliation" };
  if (run.state === "COMPLETED") return { kind: "completed" };
  if (run.state === "FAILED") return { kind: "failed", nodeId: failedNodeId(run) };
  return { kind: "running", nodeId: currentNodeId(run) };
}

function currentNodeId(run: AnyRun): string {
  return isRunV3(run) ? run.current_node_id : run.current_node.node_id;
}

/**
 * The node a failed run failed at.
 *
 * `node_rail` names it directly for a V2/V3 run. A V1 run carries no rail, so
 * the current node is read instead -- true because this engine only reaches
 * FAILED by failing the node the run's cursor was sitting on.
 */
function failedNodeId(run: AnyRun): string {
  if ("node_rail" in run) {
    const failed = run.node_rail.find((entry) => entry.state === "failed");
    if (failed !== undefined) return failed.node_id;
  }
  return currentNodeId(run);
}

function miniPipeline(run: AnyRun): readonly MiniPipelineDot[] | null {
  if (!("node_rail" in run)) return null;
  return run.node_rail.map((entry) => ({ nodeId: entry.node_id, state: entry.state }));
}
