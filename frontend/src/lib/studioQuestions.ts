/**
 * The named user question each interactive Studio-stage control answers.
 * This is the Studio map only — not a workshop-wide registry.
 */
export const studioQuestions = {
  start: {
    id: "start",
    question: "How do I start a run?"
  },
  emptyStart: {
    id: "empty-start",
    question: "What is the one next action when nothing is running?"
  },
  inboxRun: {
    id: "inbox-run",
    question: "What waits for me, and can I go there?"
  },
  project: {
    id: "project",
    question: "What is happening in this workshop, and can I open it?"
  },
  whyOneProject: {
    id: "why-one-project",
    question: "Why is there only one project?",
    hintLabel: "Why one project"
  },
  lastLandingTime: {
    id: "last-landing-time",
    question: "When exactly did the last run land?",
    hintLabel: "Exact time"
  },
  reloadStudioRuns: {
    id: "reload-studio-runs",
    question: "Can I read the board runs again?",
    readLabel: "board runs"
  },
  retryProjection: {
    id: "retry-projection",
    question: "Can I apply the attention event that failed?"
  }
} as const;

export type StudioQuestion = (typeof studioQuestions)[keyof typeof studioQuestions];
export type StudioQuestionId = StudioQuestion["id"];

export const studioQuestionAttribute = "data-studio-question";
export const studioStageSelector = ".studio-home";
export const studioInteractiveSelector = 'a[href], button, [role="button"], [role="link"]';

export type StudioControlFacts = {
  questionId: string | null;
  href: string | null;
  ariaLabel: string | null;
  tag: string;
};

export function studioControlFacts(element: Element): StudioControlFacts {
  return {
    questionId: element.getAttribute(studioQuestionAttribute),
    href: element.getAttribute("href"),
    ariaLabel: element.getAttribute("aria-label"),
    tag: element.tagName.toLowerCase()
  };
}

export function describeStudioControlFacts(facts: StudioControlFacts): string {
  const name = facts.ariaLabel ?? facts.questionId ?? "";
  return `${facts.tag}${facts.href === null ? "" : `[href="${facts.href}"]`} ${name}`.trim();
}

export function describeStudioControl(element: Element): string {
  return describeStudioControlFacts(studioControlFacts(element));
}

export function questionForStudioControlFacts(facts: StudioControlFacts): StudioQuestion | null {
  if (facts.questionId !== null) {
    return questionById(facts.questionId);
  }
  if (facts.tag === "a" && facts.href !== null) {
    if (facts.href.startsWith("/atelier/runs/")) return studioQuestions.inboxRun;
    if (facts.href === "/atelier/project") return studioQuestions.project;
  }
  if (facts.tag === "button" && facts.ariaLabel === studioQuestions.whyOneProject.hintLabel) {
    return studioQuestions.whyOneProject;
  }
  if (facts.tag === "button" && facts.ariaLabel === studioQuestions.lastLandingTime.hintLabel) {
    return studioQuestions.lastLandingTime;
  }
  if (
    facts.tag === "button" &&
    facts.ariaLabel !== null &&
    facts.ariaLabel.endsWith(` ${studioQuestions.reloadStudioRuns.readLabel}`)
  ) {
    return studioQuestions.reloadStudioRuns;
  }
  return null;
}

export function questionForStudioControl(element: Element): StudioQuestion | null {
  return questionForStudioControlFacts(studioControlFacts(element));
}

export function unansweredStudioControls(root: ParentNode): Element[] {
  return [...root.querySelectorAll(studioInteractiveSelector)].filter(
    (element) => questionForStudioControl(element) === null
  );
}

function questionById(id: string): StudioQuestion | null {
  return Object.values(studioQuestions).find((entry) => entry.id === id) ?? null;
}
