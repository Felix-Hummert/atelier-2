/**
 * The Board's own English copy. One owner, `wrapDisplayCopy` wraps every string
 * it renders.
 *
 * What a run's *state* is called is deliberately not owned here: `standingWords`
 * in `runState.ts` owns that one word, and Board, Run, History and the project
 * level all read it — so a finished run cannot be "Done" on one surface and
 * "Completed" on another (operator ruling 23.08.). This file owns only what is
 * the Board's alone: its title, its group headings and its empty state.
 */
export const studioPageCopy = {
  eyebrow: "Atelier",
  title: "Board",
  emptyTitle: "Nothing is running",
  emptyDescription: "Runs appear here the moment one starts — start one from a workflow.",
  emptyStart: "Open Workflows",
  needsYou: "Needs you",
  running: "Running",
  done: "Done today",
  why: "Why?",
  runsIncomplete: "Board runs incomplete",
  runsUnavailable: "Board runs unavailable",
  workflowNamesUnavailable: "Workflow names unavailable — showing run ids."
} as const;
