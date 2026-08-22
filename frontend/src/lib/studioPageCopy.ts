import { standingWords } from "./runState";

export const studioPageCopy = {
  eyebrow: "Atelier",
  title: "Board",
  start: "Start",
  emptyTitle: "Nothing is running",
  emptyDescription: "A workflow becomes a run, and a run is what this workshop shows.",
  emptyStart: "Start a run",
  projects: "Projects",
  chat: "Chat",
  chatUnavailable: "Unavailable",
  needsYou: "needs you",
  needYou: "need you",
  runningCount: "running",
  waitingCount: "waiting for you",
  failedCount: "failed",
  landedCount: standingWords.done.toLowerCase(),
  runsIncomplete: "Board runs incomplete",
  runsUnavailable: "Board runs unavailable"
} as const;
