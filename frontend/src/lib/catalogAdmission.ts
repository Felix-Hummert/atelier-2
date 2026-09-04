import type {
  CatalogIntakeKind,
  CatalogLineageKind,
  CockpitApi
} from "../api/client";
import { isCatalogDisplayName, problemCode } from "./catalogName";
import { publicationMutation } from "./mutationJournal";

/** The attributed actor this cockpit writes on a catalog admission event. */
export const COCKPIT_CATALOG_ACTOR = "atelier2-cockpit";

export function catalogActivatedAt(now: Date = new Date()): string {
  return now.toISOString().replace(/\.\d{3}Z$/, "Z");
}

type AdmittingApi = Pick<
  CockpitApi,
  "foundCatalogLineage" | "admitCatalogMember" | "getRevisionByName"
>;

/**
 * Name a just-published revision through the existing admission door.
 *
 * Publication does not found a lineage. This is the second HTTP act: POST
 * /catalog-lineages, or POST …/members when that authored name is already
 * held. One door serves every kind, so a workflow and an agent are named the
 * same way — under their own kind, which is what lets both carry one name. A
 * title the catalog grammar refuses is skipped so publish still succeeds.
 */
export async function admitPublishedRevision(
  api: AdmittingApi,
  kind: CatalogLineageKind,
  revisionHash: string,
  authoredName: string,
  actor: string,
  activatedAt: string
): Promise<void> {
  if (!isCatalogDisplayName(authoredName)) return;
  const admission = {
    kind,
    catalog_revision_hash: revisionHash,
    actor,
    activated_at: activatedAt
  };
  try {
    await api.foundCatalogLineage(admission);
    return;
  } catch (error) {
    if (problemCode(error) === "catalog-revision-owned") return;
    if (problemCode(error) !== "catalog-name-held") throw error;
  }
  const head = await api.getRevisionByName(kind, authoredName);
  try {
    await api.admitCatalogMember(head.lineage_id, admission);
  } catch (error) {
    if (problemCode(error) === "catalog-revision-owned") return;
    throw error;
  }
}

/**
 * Store the opaque bytes under the kind the operator declared, then use the
 * existing publish and admit doors so a workflow or agent still reaches the
 * catalog. A skill has no catalog store yet — the intake is the whole act.
 *
 * An agent's authored name lives in its frontmatter, and publication answers
 * the hash alone, so the published revision is read back for the name its
 * lineage is founded under.
 */
export async function handCatalogDocumentIn(
  api: Pick<
    CockpitApi,
    | "addLibraryDocument"
    | "publish"
    | "publishAgentDefinition"
    | "getAgentDefinitionRevision"
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
    const published = await api.publishAgentDefinition(text);
    const definition = await api.getAgentDefinitionRevision(
      published.value.agent_definition_revision_hash
    );
    await admitPublishedRevision(
      api,
      "agent_definition",
      definition.agent_definition_revision_hash,
      definition.name,
      actor,
      activatedAt
    );
    return;
  }
  const published = await api.publish(await publicationMutation(text));
  await admitPublishedRevision(
    api,
    "workflow",
    published.value.workflow_revision_hash,
    published.value.graph.name,
    actor,
    activatedAt
  );
}
