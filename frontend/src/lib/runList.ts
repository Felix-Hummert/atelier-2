import type { RunV3, WorkflowRevisionDetail } from "../api/client";
import { parseUtc } from "./when";

/**
 * Last known movement on a run: the end if the wire has one, otherwise the start.
 * The list has no other clock. A row with neither stamp has none.
 */
export function runActivityAt(run: RunV3): string | null {
  return run.ended_at ?? run.started_at ?? null;
}

/**
 * Newest activity first. Rows without a stamp stay at the end in the order
 * the durable list already gave, because inventing a time would rank them.
 */
export function newestActivityFirst(runs: readonly RunV3[]): RunV3[] {
  return [...runs].sort((left, right) => {
    const leftAt = activityMs(left);
    const rightAt = activityMs(right);
    if (leftAt === null && rightAt === null) return 0;
    if (leftAt === null) return 1;
    if (rightAt === null) return -1;
    return rightAt - leftAt;
  });
}

function activityMs(run: RunV3): number | null {
  const stamp = runActivityAt(run);
  if (stamp === null) return null;
  const ms = parseUtc(stamp).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/**
 * One entry per run, keeping the fresher read of it.
 *
 * A surface that asks several state lists at one moment gets them answered
 * separately, so a run that moves between two of those answers -- a wait that
 * opens while the started list is still on the wire -- comes back in both. The
 * higher `state_version` is the run's truth; the room shows it once.
 */
export function newestReadOfEachRun(runs: readonly RunV3[]): RunV3[] {
  const newest = new Map<string, RunV3>();
  for (const run of runs) {
    const known = newest.get(run.public_run_reference);
    if (known === undefined || known.state_version <= run.state_version) {
      newest.set(run.public_run_reference, run);
    }
  }
  return [...newest.values()];
}

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
  run: RunV3,
  workflowNames: ReadonlyMap<string, string | null> | null
): string {
  return workflowNames?.get(run.workflow_revision_hash) ?? run.run_id;
}

/** Published V3 titles keyed by the revision hash the run already carries. */
export async function workflowNamesOf(
  runs: readonly RunV3[],
  readRevision: (hash: string) => Promise<WorkflowRevisionDetail>
): Promise<ReadonlyMap<string, string>> {
  const hashes = [...new Set(runs.map((run) => run.workflow_revision_hash))];
  const names = new Map<string, string>();
  const revisions = await Promise.all(
    hashes.map(async (hash) => {
      const revision = await readRevision(hash);
      return { hash, revision };
    })
  );
  for (const { hash, revision } of revisions) {
    if (revision.workflow_revision_hash !== hash) {
      throw new Error("a V3 run received a different workflow revision");
    }
    // The wire type admits only format 3, so this guards corrupt durable state
    // alone -- a V1/V2 row the server should never serve again (#901 slice 5)
    // -- and stays the one remaining defense of its kind.
    if (revision.graph.workflow_format_version !== 3) {
      throw new Error("a V3 run referenced a workflow revision of another format");
    }
    names.set(hash, revision.graph.name);
  }
  return names;
}
