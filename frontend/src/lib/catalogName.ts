import { CockpitRequestError, type Problem } from "../api/client";

/**
 * Whether a published title can be a catalog display name.
 *
 * Owner: `CatalogLineageDisplayName` in `atelier2.contracts.catalog_v3`.
 * This is the cockpit's reading of that contract, not a second grammar.
 */
const CATALOG_DISPLAY_NAME = /^[a-z][a-z0-9._-]*$/;
const SHA256_HEX = /^[0-9a-f]{64}$/;
const MAXIMUM_CATALOG_DISPLAY_NAME_CHARACTERS = 128;
const PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:";

export type CatalogNameState =
  | { kind: "admitted"; revisionHash: string }
  | { kind: "unlisted" }
  | { kind: "unnamable" }
  | { kind: "retired" };

export function isCatalogDisplayName(value: string): boolean {
  return (
    value.length >= 1 &&
    value.length <= MAXIMUM_CATALOG_DISPLAY_NAME_CHARACTERS &&
    CATALOG_DISPLAY_NAME.test(value) &&
    SHA256_HEX.exec(value) === null
  );
}

export function catalogNameStateOf(
  name: string,
  askByName: (name: string) => Promise<{ workflow_revision_hash: string }>
): Promise<CatalogNameState> {
  if (!isCatalogDisplayName(name)) {
    return Promise.resolve({ kind: "unnamable" });
  }
  return askByName(name).then(
    (head) => ({ kind: "admitted" as const, revisionHash: head.workflow_revision_hash }),
    (error: unknown) => {
      const code = problemCode(error);
      if (code === "catalog-name-not-found") return { kind: "unlisted" as const };
      if (code === "catalog-lineage-retired") return { kind: "retired" as const };
      throw error;
    }
  );
}

export function problemCode(error: unknown): string | null {
  if (!(error instanceof CockpitRequestError) || error.problem === null) return null;
  return problemTypeCode(error.problem);
}

export function problemTypeCode(problem: Pick<Problem, "type">): string | null {
  if (!problem.type.startsWith(PROBLEM_TYPE_PREFIX)) return null;
  return problem.type.slice(PROBLEM_TYPE_PREFIX.length);
}
