import { retryLabel } from "./readStateCopy";
import { workbenchPageCopy } from "./workbenchPageCopy";

/**
 * Inventory of interactive Workbench controls: each rendered control has an
 * entry, and each entry is shaped as a question. This is the Workbench map
 * only — not a workshop-wide registry, and not a judgement that the question
 * is the right one.
 */
export const workbenchQuestions = {
  emptyStart: {
    id: "empty-start",
    question: "What is the one next action when nothing has happened yet?"
  },
  openRun: {
    id: "open-run",
    question: "Can I open a run to see it or answer what it needs?"
  },
  reloadWorkbenchRuns: {
    id: "reload-workbench-runs",
    question: "Can I read the workbench runs again?"
  },
  retryProjection: {
    id: "retry-projection",
    question: "Can I read what changed, after that read failed?"
  },
  saySomething: {
    id: "say-something",
    question: "Can I tell the workshop what I want?"
  },
  answerDecision: {
    id: "answer-decision",
    question: "Can I answer, or send again, a decision that waits on me?"
  }
} as const;

export type WorkbenchQuestion = (typeof workbenchQuestions)[keyof typeof workbenchQuestions];

export const workbenchQuestionAttribute = "data-workbench-question";
export const workbenchStageSelector = ".workbench";
export const workbenchInteractiveSelector = 'a[href], button, [role="button"], [role="link"]';

export type WorkbenchControlFacts = {
  questionId: string | null;
  href: string | null;
  ariaLabel: string | null;
  tag: string;
};

export function workbenchControlFacts(element: Element): WorkbenchControlFacts {
  return {
    questionId: element.getAttribute(workbenchQuestionAttribute),
    href: element.getAttribute("href"),
    ariaLabel: element.getAttribute("aria-label"),
    tag: element.tagName.toLowerCase()
  };
}

export function describeWorkbenchControlFacts(facts: WorkbenchControlFacts): string {
  const name = facts.ariaLabel ?? facts.questionId ?? "";
  return `${facts.tag}${facts.href === null ? "" : `[href="${facts.href}"]`} ${name}`.trim();
}

export function describeWorkbenchControl(element: Element): string {
  return describeWorkbenchControlFacts(workbenchControlFacts(element));
}

export function questionForWorkbenchControlFacts(
  facts: WorkbenchControlFacts
): WorkbenchQuestion | null {
  if (facts.questionId !== null) {
    return questionById(facts.questionId);
  }
  if (facts.tag === "a" && facts.href !== null && facts.href.startsWith("/atelier/runs/")) {
    return workbenchQuestions.openRun;
  }
  if (
    facts.tag === "button" &&
    facts.ariaLabel === retryLabel(workbenchPageCopy.runsLabel)
  ) {
    return workbenchQuestions.reloadWorkbenchRuns;
  }
  return null;
}

export function questionForWorkbenchControl(element: Element): WorkbenchQuestion | null {
  return questionForWorkbenchControlFacts(workbenchControlFacts(element));
}

export function unansweredWorkbenchControls(root: ParentNode): Element[] {
  return [...root.querySelectorAll(workbenchInteractiveSelector)].filter(
    (element) => questionForWorkbenchControl(element) === null
  );
}

function questionById(id: string): WorkbenchQuestion | null {
  return Object.values(workbenchQuestions).find((entry) => entry.id === id) ?? null;
}
