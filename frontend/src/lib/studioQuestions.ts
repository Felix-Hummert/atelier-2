/**
 * The named user question each interactive Board control answers.
 * This is the Board map only — not a workshop-wide registry.
 */
export const studioQuestions = {
  emptyStart: {
    id: "empty-start",
    question: "What is the one next action when nothing is running?"
  },
  openRun: {
    id: "open-run",
    question: "Can I open a run to see it or answer what it needs?"
  },
  reloadStudioRuns: {
    id: "reload-studio-runs",
    question: "Can I read the board runs again?",
    readLabel: "board runs"
  },
  retryProjection: {
    id: "retry-projection",
    question: "Can I apply the attention event that failed?"
  },
  /**
   * Not a Board control: `When.svelte` reuses this hint label wherever it
   * renders a relative time, on the run page as much as here. It stays on
   * this map because that is the one place a relative-time label is named,
   * not because the run page answers to the Board's question set.
   */
  lastLandingTime: {
    id: "last-landing-time",
    question: "When exactly did this happen?",
    hintLabel: "Exact time"
  }
} as const;

export type StudioQuestion = (typeof studioQuestions)[keyof typeof studioQuestions];
export type StudioQuestionId = StudioQuestion["id"];

export const studioQuestionAttribute = "data-studio-question";
export const studioStageSelector = ".board-page";
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
  if (facts.tag === "a" && facts.href !== null && facts.href.startsWith("/atelier/runs/")) {
    return studioQuestions.openRun;
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
