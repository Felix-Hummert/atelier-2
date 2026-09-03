/**
 * The one substring-match owner behind every free-text filter on these
 * rooms' surfaces (the catalog search, the start sheet's work-item picker):
 * locale-aware, case-insensitive, and blank-matches-everything so an empty
 * field never hides content.
 */
export function matchesSearchTerm(candidates: readonly (string | null)[], term: string): boolean {
  const normalizedTerm = term.trim().toLocaleLowerCase();
  if (normalizedTerm === "") return true;
  return candidates
    .filter((value): value is string => value !== null)
    .some((value) => value.toLocaleLowerCase().includes(normalizedTerm));
}
