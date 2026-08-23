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
  workflowNamesUnavailable: "Workflow names unavailable — showing run ids.",
  /**
   * The inline decision affordance a boolean/enum wait gate carries on its own
   * Board card (#572): the same audited POST the run page uses, answered in
   * two clicks instead of a visit. A free-text gate never offers this --
   * `needsWrittenAnswer` names that honestly instead.
   */
  answerHere: "Answer here",
  answerHereLooking: "Looking…",
  answerHereUnavailable: "This gate could not be read here.",
  needsWrittenAnswer: "This needs a written answer.",
  openToAnswer: "Open the run to answer",
  /**
   * The quiet, subordinate door to the whole run, shown beside a card that
   * already offers its own inline "Answer here" -- distinct wording from
   * `openToAnswer` on purpose, since this row never lacks an inline answer
   * to name (Leonardo-Gate 23.08.).
   */
  openRun: "Open run"
} as const;
