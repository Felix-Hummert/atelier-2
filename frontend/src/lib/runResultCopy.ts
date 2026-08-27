/**
 * Words the readable-result surface speaks (#716): the node panel's Result
 * tab and the wait card's predecessor context share this one owner because
 * they share the one rendering (`ReadableResult.svelte`), never a duplicated
 * string per surface.
 *
 * Folding this into `runPageCopy.ts` once both own the same run page is a
 * named follow-up on #716's body, not a claim this file makes permanent.
 * `unreadable` below and `runPageCopy.answerContextUnreadable` say the same
 * thing in two words for two different surfaces (a stored value bytes could
 * not decode); the same follow-up folds them into one word once they share
 * an owner.
 */

export const runResultCopy = {
  /** The disclosure that reveals the exact bytes behind a readable form. */
  exactText: "Exact text",
  /** A stored value these bytes could not decode as UTF-8 text. */
  unreadable: "This could not be read as text."
} as const;
