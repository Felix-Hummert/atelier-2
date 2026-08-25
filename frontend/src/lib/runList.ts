import { isRunV3, type AnyRun, type WorkflowGraph, type WorkflowRevisionDetail } from "../api/client";
import { parseUtc } from "./when";

/** A published revision's graph, narrowed to the one format that carries `node_previews`. */
export type WorkflowGraphV3 = Extract<WorkflowGraph, { workflow_format_version: 3 }>;

/**
 * A V3 revision's own graph, or the loud refusal a V3 run's revision must
 * never actually trigger: the store only lets a V3 run point at a V3
 * revision, so a caller reaching this on a real run has found a corrupt
 * reference, not an expected shape to handle quietly.
 */
export function v3WorkflowGraph(revision: WorkflowRevisionDetail): WorkflowGraphV3 {
  if (revision.graph.workflow_format_version !== 3) {
    throw new Error("a V3 run referenced a workflow revision of another format");
  }
  return revision.graph;
}

/**
 * Last known movement on a run: the end if the wire has one, otherwise the start.
 * The list has no other clock. V1/V2 rows and a V3 row with neither stamp have none.
 */
export function runActivityAt(run: AnyRun): string | null {
  if (!isRunV3(run)) return null;
  return run.ended_at ?? run.started_at ?? null;
}

/**
 * Newest activity first. Rows without a stamp stay at the end in the order
 * the durable list already gave, because inventing a time would rank them.
 */
export function newestActivityFirst(runs: readonly AnyRun[]): AnyRun[] {
  return [...runs].sort((left, right) => {
    const leftAt = activityMs(left);
    const rightAt = activityMs(right);
    if (leftAt === null && rightAt === null) return 0;
    if (leftAt === null) return 1;
    if (rightAt === null) return -1;
    return rightAt - leftAt;
  });
}

function activityMs(run: AnyRun): number | null {
  const stamp = runActivityAt(run);
  if (stamp === null) return null;
  const ms = parseUtc(stamp).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/**
 * The distinct V3 revisions a batch of runs refers to, read once per hash
 * regardless of how many runs share it -- the one fetch every reader that
 * needs a run's published revision (its name, its graph shape) builds on.
 */
export async function workflowRevisionsOf(
  runs: readonly AnyRun[],
  readRevision: (hash: string) => Promise<WorkflowRevisionDetail>
): Promise<ReadonlyMap<string, WorkflowRevisionDetail>> {
  const hashes = [
    ...new Set(runs.filter(isRunV3).map((run) => run.workflow_revision_hash))
  ];
  const revisions = await Promise.all(
    hashes.map(async (hash) => {
      const revision = await readRevision(hash);
      if (revision.workflow_revision_hash !== hash) {
        throw new Error("a V3 run received a different workflow revision");
      }
      return [hash, revision] as const;
    })
  );
  return new Map(revisions);
}

/** Published V3 titles keyed by the revision hash the run already carries. */
export async function workflowNamesOf(
  runs: readonly AnyRun[],
  readRevision: (hash: string) => Promise<WorkflowRevisionDetail>
): Promise<ReadonlyMap<string, string>> {
  const revisions = await workflowRevisionsOf(runs, readRevision);
  const names = new Map<string, string>();
  for (const [hash, revision] of revisions) {
    names.set(hash, v3WorkflowGraph(revision).name);
  }
  return names;
}
