import {
  describeWorkbenchControlFacts,
  questionForWorkbenchControlFacts,
  workbenchQuestionAttribute,
  type WorkbenchControlFacts,
  type WorkbenchQuestion
} from "../../src/lib/workbenchQuestions";

/**
 * Reading the Workbench's rendered controls, for the two test layers that
 * check every control answers a question: the jsdom room test walks real
 * elements, the browser test collects the same facts inside the page. Only
 * the facts half is production's -- an `Element` never reaches the product.
 */
export const workbenchStageSelector = ".workbench";
export const workbenchInteractiveSelector = 'a[href], button, [role="button"], [role="link"]';

export function workbenchControlFacts(element: Element): WorkbenchControlFacts {
  return {
    questionId: element.getAttribute(workbenchQuestionAttribute),
    href: element.getAttribute("href"),
    ariaLabel: element.getAttribute("aria-label"),
    tag: element.tagName.toLowerCase()
  };
}

export function describeWorkbenchControl(element: Element): string {
  return describeWorkbenchControlFacts(workbenchControlFacts(element));
}

export function questionForWorkbenchControl(element: Element): WorkbenchQuestion | null {
  return questionForWorkbenchControlFacts(workbenchControlFacts(element));
}

export function unansweredWorkbenchControls(root: ParentNode): Element[] {
  return [...root.querySelectorAll(workbenchInteractiveSelector)].filter(
    (element) => questionForWorkbenchControl(element) === null
  );
}
