import type {
  CatalogIntakeKind,
  CockpitApi,
  WorkflowRevisionDetail
} from "../api/client";
import { isCatalogDisplayName, problemCode } from "./catalogName";
import { publicationMutation } from "./mutationJournal";

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
  if (!isCatalogDisplayName(revision.graph.name)) return;
  try {
    await api.foundCatalogLineage({
      workflow_revision_hash: revision.workflow_revision_hash,
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
      workflow_revision_hash: revision.workflow_revision_hash,
      actor,
      activated_at: activatedAt
    });
  } catch (error) {
    if (problemCode(error) === "catalog-revision-owned") return;
    throw error;
  }
}

/**
 * Store the opaque bytes under the kind the operator declared, then use the
 * existing publish and admit doors so a workflow or agent still reaches the
 * catalog. A skill has no catalog store yet — the intake is the whole act.
 */
export async function handCatalogDocumentIn(
  api: Pick<
    CockpitApi,
    | "addLibraryDocument"
    | "publish"
    | "publishAgentDefinition"
    | "foundCatalogLineage"
    | "admitCatalogMember"
    | "getRevisionByName"
  >,
  document: Uint8Array,
  kind: CatalogIntakeKind,
  actor: string,
  activatedAt: string
): Promise<void> {
  await api.addLibraryDocument(document, kind, actor, activatedAt);
  if (kind === "skill") return;
  const text = new TextDecoder("utf-8", { fatal: true }).decode(document);
  if (kind === "agent") {
    await api.publishAgentDefinition(text);
    return;
  }
  const published = await api.publish(await publicationMutation(text));
  await admitPublishedRevision(api, published.value, actor, activatedAt);
}
