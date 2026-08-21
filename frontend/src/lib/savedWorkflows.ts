import type { WorkflowRevisionDetail, WorkflowRevisionSummary } from "../api/client";

/**
 * One picker row: either a published name with every listed revision of it,
 * or one unnamed document that cannot share a name.
 *
 * `revisions[0]` is the default choice — the catalog head when the caller
 * supplied it, otherwise the first listed member of that name.
 */
export interface SavedWorkflowRow {
  key: string;
  name: string | null;
  revisions: WorkflowRevisionSummary[];
}

export function groupSavedWorkflows(
  revisions: readonly WorkflowRevisionSummary[],
  newestByName: Readonly<Record<string, string>> = {}
): SavedWorkflowRow[] {
  const grouped = new Map<string, WorkflowRevisionSummary[]>();
  const order: string[] = [];

  for (const revision of revisions) {
    const key =
      revision.name === null
        ? `unnamed:${revision.workflow_revision_hash}`
        : `named:${revision.name}`;
    const existing = grouped.get(key);
    if (existing === undefined) {
      grouped.set(key, [revision]);
      order.push(key);
      continue;
    }
    existing.push(revision);
  }

  return order.map((key) => {
    const group = grouped.get(key) ?? [];
    const name = group[0]?.name ?? null;
    const newest = name === null ? undefined : newestByName[name];
    return {
      key,
      name,
      revisions: withHeadFirst(group, newest)
    };
  });
}

export function namesWithSeveralRevisions(
  revisions: readonly WorkflowRevisionSummary[]
): string[] {
  const counts = new Map<string, number>();
  for (const revision of revisions) {
    if (revision.name === null) continue;
    counts.set(revision.name, (counts.get(revision.name) ?? 0) + 1);
  }
  return [...counts.entries()].filter(([, count]) => count > 1).map(([name]) => name);
}

export function selectedRevisionOf(
  row: SavedWorkflowRow,
  selectedHash?: string
): WorkflowRevisionSummary {
  const fallback = row.revisions[0];
  if (fallback === undefined) {
    throw new Error("a saved-workflow row must carry at least one revision");
  }
  if (selectedHash === undefined) return fallback;
  return row.revisions.find((item) => item.workflow_revision_hash === selectedHash) ?? fallback;
}

export function revisionChoiceLabel(
  revision: WorkflowRevisionSummary,
  newestHash: string
): string {
  return revision.workflow_revision_hash === newestHash ? "Latest" : "Earlier";
}

/** The roles authored by an executable workflow document, once each. */
export function agentRolesOf(graph: WorkflowRevisionDetail["graph"]): string[] {
  if (graph.workflow_format_version === 3) return [...new Set(graph.agent_roles)];
  if (graph.workflow_format_version === 2) {
    return [...new Set(graph.nodes.filter((node) => node.type === "agent").map((node) => node.role))];
  }
  return [];
}

function withHeadFirst(
  revisions: readonly WorkflowRevisionSummary[],
  newestHash: string | undefined
): WorkflowRevisionSummary[] {
  if (newestHash === undefined) return [...revisions];
  const head = revisions.find((item) => item.workflow_revision_hash === newestHash);
  if (head === undefined) return [...revisions];
  return [head, ...revisions.filter((item) => item.workflow_revision_hash !== newestHash)];
}
