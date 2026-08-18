import type { CockpitApi, WorkflowRevisionDetail } from "../api/client";
import { isCatalogDisplayName, problemCode } from "./catalogName";

/** The attributed actor this cockpit writes on a catalog admission event. */
export const COCKPIT_CATALOG_ACTOR = "atelier2-cockpit";

export function catalogActivatedAt(now: Date = new Date()): string {
  return now.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/**
 * Name a just-published V3 revision through the existing admission door.
 *
 * Publication does not found a lineage. This is the second HTTP act: POST
 * /workflow-lineages, or POST …/members when that authored name is already
 * held. A title the catalog grammar refuses is skipped so publish still
 * succeeds.
 */
export async function admitPublishedRevision(
  api: Pick<CockpitApi, "foundCatalogLineage" | "admitCatalogMember" | "getRevisionByName">,
  revision: WorkflowRevisionDetail,
  actor: string,
  activatedAt: string
): Promise<void> {
  if (revision.graph.format_version !== 3) return;
  if (!isCatalogDisplayName(revision.graph.name)) return;
  try {
    await api.foundCatalogLineage({
      revision_hash: revision.revision_hash,
      actor,
      activated_at: activatedAt
    });
    return;
  } catch (error) {
    if (problemCode(error) === "catalog-revision-owned") return;
    if (problemCode(error) !== "catalog-name-held") throw error;
  }
  const head = await api.getRevisionByName(revision.graph.name);
  try {
    await api.admitCatalogMember(head.lineage_id, {
      revision_hash: revision.revision_hash,
      actor,
      activated_at: activatedAt
    });
  } catch (error) {
    if (problemCode(error) === "catalog-revision-owned") return;
    throw error;
  }
}
