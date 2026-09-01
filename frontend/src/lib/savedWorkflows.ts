import type { WorkflowRevisionDetail, WorkflowRevisionSummary } from "../api/client";

/**
 * One picker row: a published name with every listed revision of it.
 *
 * `revisions[0]` is the default choice — the catalog head when the caller
 * supplied it, otherwise the first listed member of that name.
 */
export interface SavedWorkflowRow {
  key: string;
  name: string;
  revisions: WorkflowRevisionSummary[];
}

export function groupSavedWorkflows(
  revisions: readonly WorkflowRevisionSummary[],
  newestByName: Readonly<Record<string, string>> = {}
): SavedWorkflowRow[] {
  const grouped = new Map<string, WorkflowRevisionSummary[]>();
  const order: string[] = [];

  for (const revision of revisions) {
    const key = `named:${revision.name}`;
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
    const name = group[0]?.name ?? "";
    return {
      key,
      name,
      revisions: withHeadFirst(group, newestByName[name])
    };
  });
}

/** The roles authored by an executable workflow document, once each. */
export function agentRolesOf(graph: WorkflowRevisionDetail["graph"]): string[] {
  return [...new Set(graph.agent_roles)];
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
