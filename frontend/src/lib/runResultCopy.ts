/**
 * Words the readable-result surface speaks (#716): the run page's own
 * outcome and the node panel's Result tab share this one owner because they
 * share the one rendering, never a duplicated string per surface.
 *
 * A separate file rather than an addition to `runPageCopy.ts`: that owner is
 * mid-edit under another lane's exact-scope claim when this fix lands, so a
 * new surface's copy gets its own file the way every other page-scoped copy
 * already does (`historyPageCopy.ts`, `workflowsPageCopy.ts`, …) rather than
 * colliding with a locked file or inventing a second convention.
 */

export const runResultCopy = {
  raw: "Raw"
} as const;
