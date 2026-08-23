/**
 * Copy the project surface renders.
 *
 * The project is the context above the four rail destinations, not a fifth
 * one — so this page answers only what no destination answers: what this
 * project is called, how much work it holds, where that work is read, and
 * which agent the house reaches for by default. The run list it used to carry
 * is the Board's job while work runs and History's once it lands; keeping a
 * third copy here was the same statement three times (#536).
 */
export const projectPageCopy = {
  workTitle: "Work in this project",
  noRuns: "No runs in this project yet.",
  noRunsNext: "Start one from a workflow.",
  referencesTitle: "Where that work is read",
  board: "Board",
  boardDescription: "What is running and what needs you.",
  history: "History",
  historyDescription: "What has finished.",
  workflows: "Workflows",
  workflowsDescription: "What the house can do, and where a run starts.",
  occupancyEyebrow: "Project defaults",
  occupancyTitle: "Who does the work",
  occupancyDescription:
    "The agent this project reaches for by default when a workflow asks for a role.",
  occupancyUnavailable: "Project defaults unavailable",
  runsUnavailable: "Project runs unavailable",
  runsIncomplete: "Project runs incomplete"
} as const;
