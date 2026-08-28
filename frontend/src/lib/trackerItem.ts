/**
 * Tracker-item identity as History may name it: the adapter\'s grammar for a
 * reference, and a URL to that issue when the project\'s source connection
 * can form one. Never a title — titles come from tracker enrichment, not
 * from this module.
 */

const GITHUB_SOURCE_KIND = "github";
const GITHUB_REFERENCE_PREFIX = "gh:";
const GITLAB_REFERENCE_PREFIX = "gl:";

export type TrackerSourceConnection = {
  source_kind: string;
  source_address: string;
};

/** The adapter\'s own spelling of a tracker reference (`gh:567` → `#567`). */
export function trackerItemLabel(reference: string): string {
  if (reference.startsWith(GITHUB_REFERENCE_PREFIX)) {
    return `#${reference.slice(GITHUB_REFERENCE_PREFIX.length)}`;
  }
  if (reference.startsWith(GITLAB_REFERENCE_PREFIX)) {
    return `!${reference.slice(GITLAB_REFERENCE_PREFIX.length)}`;
  }
  return reference;
}

/**
 * The issue URL for one tracker reference, or null when this connection
 * cannot honestly form one (unknown kind, malformed address, or a
 * reference that does not belong to that kind).
 */
export function trackerItemHref(
  reference: string,
  source: TrackerSourceConnection | null
): string | null {
  if (source === null) return null;
  if (
    source.source_kind === GITHUB_SOURCE_KIND &&
    reference.startsWith(GITHUB_REFERENCE_PREFIX)
  ) {
    const repository = githubRepository(source.source_address);
    if (repository === null) return null;
    const number = reference.slice(GITHUB_REFERENCE_PREFIX.length);
    if (number.length === 0) return null;
    return `https://github.com/${repository.owner}/${repository.name}/issues/${number}`;
  }
  return null;
}

/**
 * A complete work-item order document\'s tracker reference, or null.
 *
 * The house schema\'s fields are closed: body, change marker, digest, kind,
 * observed_at, reference. This reads only `reference`. The body is never a
 * title and never returned.
 */
export function workItemReferenceFromOrderDocument(value: unknown): string | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record);
  if (keys.length !== WORK_ITEM_ORDER_FIELDS.length) return null;
  if (!WORK_ITEM_ORDER_FIELDS.every((field) => keys.includes(field))) return null;
  if (!WORK_ITEM_ORDER_FIELDS.every((field) => typeof record[field] === "string")) {
    return null;
  }
  if (record.kind !== "issue" && record.kind !== "change_request") return null;
  const reference = record.reference;
  return typeof reference === "string" && reference.length > 0 ? reference : null;
}

/**
 * The work-item reference pinned on a node job, read only from a complete
 * house-schema order document in the job. Other JSON in the job is ignored.
 */
export function workItemReferenceFromJob(job: string): string | null {
  for (const block of job.split("\n\n")) {
    const start = block.indexOf("{");
    if (start < 0) continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(block.slice(start));
    } catch {
      continue;
    }
    const reference = workItemReferenceFromOrderDocument(parsed);
    if (reference !== null) return reference;
  }
  return null;
}

const WORK_ITEM_ORDER_FIELDS = [
  "body",
  "change_marker",
  "digest",
  "kind",
  "observed_at",
  "reference"
] as const;

function githubRepository(address: string): { owner: string; name: string } | null {
  const at = address.indexOf("@");
  const repositoryPart = at < 0 ? address : address.slice(0, at);
  const slash = repositoryPart.indexOf("/");
  if (slash <= 0 || slash === repositoryPart.length - 1) return null;
  const owner = repositoryPart.slice(0, slash);
  const name = repositoryPart.slice(slash + 1);
  if (owner.length === 0 || name.length === 0 || name.includes("/")) return null;
  return { owner, name };
}
