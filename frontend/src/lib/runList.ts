import { isRunV3, type AnyRun, type WorkflowRevisionDetail } from "../api/client";
import { parseUtc } from "./when";

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

/** Published V3 titles keyed by the revision hash the run already carries. */
export async function workflowNamesOf(
  runs: readonly AnyRun[],
  readRevision: (hash: string) => Promise<WorkflowRevisionDetail>
): Promise<ReadonlyMap<string, string>> {
  const hashes = [
    ...new Set(runs.filter(isRunV3).map((run) => run.workflow_revision_hash))
  ];
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
    if (revision.graph.workflow_format_version !== 3) {
      throw new Error("a V3 run referenced a workflow revision of another format");
    }
    names.set(hash, revision.graph.name);
  }
  return names;
}
