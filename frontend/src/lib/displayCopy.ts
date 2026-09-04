/**
 * The display-string transform the quality contract can see.
 *
 * Owned English lives with its owner (`WORKSHOP_DESTINATIONS`, `stateLabels`
 * in `stateMarkCopy`).
 * This wrapper is how a surface proves it read that owner: under
 * `?pseudo-locale=1` the same string comes back lengthened, so a hardcoded
 * copy of the English stays visible as itself.
 */

const PSEUDO_LOCALE_QUERY = "pseudo-locale";

export function wrapDisplayCopy(text: string): string {
  if (!pseudoLocaleIsOn()) {
    return text;
  }
  return `[[[ ${text} ]]]`;
}

function pseudoLocaleIsOn(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  return new URLSearchParams(window.location.search).has(PSEUDO_LOCALE_QUERY);
}
