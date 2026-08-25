/**
 * Words the readable-result surface speaks (#716): the run page's own
 * outcome banner and the node panel's Result tab share this one owner
 * because they share the one rendering (`ReadableResult.svelte`), never a
 * duplicated string per surface.
 *
 * Folding this into `runPageCopy.ts` once both own the same run page is a
 * named follow-up on #716's body, not a claim this file makes permanent.
 */

export const runResultCopy = {
  /** The disclosure that reveals the exact bytes behind a readable form. */
  exactText: "Exact text",
  /** What the node panel's Result tab says for the run's own sink node, whose answer the banner above already shows. */
  shownAbove: "Shown above",
  /** A stored value these bytes could not decode as UTF-8 text. */
  unreadable: "This could not be read as text."
} as const;
