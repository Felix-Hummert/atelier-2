import type {
  AgentDefinitionRevisionListItem,
  WorkflowRevisionSummary
} from "../api/client";
import { catalogPageCopy } from "./catalogPageCopy";
import type { CatalogNameState } from "./catalogName";
import { catalogHeadsOf, isCatalogDisplayName } from "./catalogName";
import { humanStartRefusal } from "./humanRefusal";
import { groupSavedWorkflows } from "./savedWorkflows";

/**
 * What the catalog says about one entry in one glance.
 *
 * Three states, because three are what an operator does something different
 * about: start it, admit it, or fix the document. A published revision that is
 * not executable can never become startable by admission, so it never wears the
 * admission's words.
 */
export type CatalogEntryState =
  | { kind: "startable" }
  | { kind: "not-admitted" }
  | { kind: "not-executable"; reason: string };

export interface CatalogWorkflowRow {
  revisionHash: string;
  title: string;
  /**
   * The document's own declared name, `null` for the same revisions `title`
   * falls back to a placeholder for. The Details door (#695) needs the real
   * name, never the placeholder, to build the workflow detail page's path.
   */
  name: string | null;
  description: string | null;
  /**
   * `null` where this room cannot honestly answer: the catalog is asked by
   * name, and a document that declares none may still be an admitted member
   * under a name only its lineage knows. Saying "not in the catalog yet" there
   * would be a guess dressed as a verdict.
   */
  state: CatalogEntryState | null;
  /** Whether the admission door can take this revision at all. */
  admittable: boolean;
  /**
   * A published sibling of this same name exists that the catalog has not
   * admitted -- the live duplicate-card finding (#659, sharpened by #684):
   * one card carries the admitted head, and this is the only trace its
   * unadmitted sibling leaves, instead of a second card wearing the same
   * name.
   */
  newerRevisionAvailable: boolean;
}

export interface CatalogAgentRow {
  revisionHash: string;
  title: string;
  description: string;
  /**
   * Which provider this agent belongs to, and never a claim it is portable.
   *
   * An imported agent is passed through whole: the atelier translates nothing,
   * so what a file says is a fact about one provider's runtime, not a
   * capability this workshop can offer to another. Neutrality lives in a
   * workflow's role and its casting, one level above this row.
   */
  provider: string;
}

/**
 * The workflow tiles and their group-head count are one projection.
 *
 * A library lists published revisions, but the Catalog deliberately collapses
 * admitted siblings into one tile. Exposing the count alongside the rows keeps
 * the group head from accidentally counting the library's revisions instead
 * of the tiles a person can see.
 */
export interface CatalogWorkflowTiles {
  rows: CatalogWorkflowRow[];
  count: number;
}

/**
 * The facts that stand under an entry's name, in reading order.
 *
 * Provenance is the first of them and today always the same sentence: nothing
 * in this build records where bytes came from, and every published revision got
 * here by hand. When the Git link (#660) starts recording a source, ref, commit
 * and path, it replaces that one sentence -- the row's shape does not change.
 */
export function catalogRowFacts(): readonly string[] {
  return [catalogPageCopy.provenanceManual];
}

/**
 * One row per published revision -- except a name the catalog has already
 * admitted, which collapses every revision under that name into its one
 * admitted card, the sibling revisions marked rather than repeated.
 *
 * Grouping by name is grouping by lineage: the catalog contract lets exactly
 * one lineage hold a display name, so a second published revision under an
 * already-admitted name is that lineage's own unadmitted sibling, not an
 * unrelated document that happens to share a title -- the live duplicate-card
 * finding (#659, sharpened by #684). Before any revision of a name is
 * admitted there is no head to collapse onto yet, so every revision still
 * keeps its own row and its own "not in the catalog yet" verdict.
 */
export function catalogWorkflowRows(
  revisions: readonly WorkflowRevisionSummary[],
  catalogByName: Readonly<Record<string, CatalogNameState>>
): CatalogWorkflowRow[] {
  const admittedHeads = catalogHeadsOf(revisions, catalogByName) ?? {};
  return groupSavedWorkflows(revisions, admittedHeads).flatMap((row) => {
    const isAdmittedName = row.name !== null && admittedHeads[row.name] !== undefined;
    const kept = isAdmittedName ? row.revisions.slice(0, 1) : row.revisions;
    return kept.map((revision, index) => ({
      revisionHash: revision.workflow_revision_hash,
      title: revision.name ?? catalogPageCopy.unnamedWorkflow,
      name: revision.name,
      description: revision.description,
      state: workflowEntryState(revision, catalogByName),
      admittable: isAdmittable(revision),
      newerRevisionAvailable: index === 0 && isAdmittedName && row.revisions.length > 1
    }));
  });
}

export function catalogWorkflowTiles(
  revisions: readonly WorkflowRevisionSummary[],
  catalogByName: Readonly<Record<string, CatalogNameState>>
): CatalogWorkflowTiles {
  const rows = catalogWorkflowRows(revisions, catalogByName);
  return { rows, count: rows.length };
}

export function catalogAgentRows(
  items: readonly AgentDefinitionRevisionListItem[]
): CatalogAgentRow[] {
  return items.map((item) => ({
    revisionHash: item.agent_definition_revision_hash,
    title: item.name,
    description: item.description,
    provider: catalogPageCopy.agentProviderClaude
  }));
}

/**
 * Admission takes a name, so a revision without one cannot be offered it.
 *
 * The grammar is the catalog's own (`isCatalogDisplayName`), not a guess: a
 * title the store would refuse must not wear a button that promises otherwise.
 */
function isAdmittable(revision: WorkflowRevisionSummary): boolean {
  return (
    revision.executable &&
    revision.name !== null &&
    isCatalogDisplayName(revision.name)
  );
}

function workflowEntryState(
  revision: WorkflowRevisionSummary,
  catalogByName: Readonly<Record<string, CatalogNameState>>
): CatalogEntryState | null {
  if (!revision.executable) {
    return {
      kind: "not-executable",
      reason: humanStartRefusal(revision.not_executable_reason ?? "")
    };
  }
  if (revision.name === null) {
    return null;
  }
  const held = catalogByName[revision.name];
  if (held?.kind === "admitted" && held.revisionHash === revision.workflow_revision_hash) {
    return { kind: "startable" };
  }
  return { kind: "not-admitted" };
}
