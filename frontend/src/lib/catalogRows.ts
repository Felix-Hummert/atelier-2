import type {
  AgentDefinitionRevisionListItem,
  WorkflowRevisionSummary
} from "../api/client";
import { catalogPageCopy } from "./catalogPageCopy";
import type { CatalogNameState } from "./catalogName";
import { isCatalogDisplayName } from "./catalogName";
import { shortFingerprint } from "./fingerprint";
import { humanStartRefusal } from "./humanRefusal";

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
 * The facts that stand under an entry's name, in reading order.
 *
 * Provenance is the first of them and today always the same sentence: nothing
 * in this build records where bytes came from, and every published revision got
 * here by hand. When the Git link (#660) starts recording a source, ref, commit
 * and path, it replaces that one sentence and may append the drift line
 * ("newer version available") — the row's shape does not change for either.
 */
export function catalogRowFacts(revisionHash: string): readonly string[] {
  return [catalogPageCopy.provenanceManual, shortFingerprint(revisionHash)];
}

export function catalogWorkflowRows(
  revisions: readonly WorkflowRevisionSummary[],
  catalogByName: Readonly<Record<string, CatalogNameState>>
): CatalogWorkflowRow[] {
  return revisions.map((revision) => ({
    revisionHash: revision.workflow_revision_hash,
    title: revision.name ?? catalogPageCopy.unnamedWorkflow,
    description: revision.description,
    state: workflowEntryState(revision, catalogByName),
    admittable: isAdmittable(revision)
  }));
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
