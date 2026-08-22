/**
 * Copy the Workflows catalog surface renders: the list of named published
 * workflows and the still-graph detail behind each one (mockup v5 §04).
 *
 * One owner per screen, the same convention `studioPageCopy` and
 * `runPageCopy` already hold to, so `?pseudo-locale=1` (`wrapDisplayCopy`)
 * can prove every string here has a source instead of a second hardcoded copy
 * inline in a page.
 */
export const workflowsPageCopy = {
  eyebrow: "Atelier",
  title: "Workflows",
  emptyTitle: "No named workflows yet",
  emptyDescription:
    "A published workflow becomes a card here once it carries a name.",
  listUnavailable: "Workflows unavailable",
  listIncomplete: "Workflows incomplete",
  noDescription: "No description.",
  start: "Start",
  detailUnavailable: "Workflow detail unavailable",
  notFoundTitle: "Workflow not found",
  notFoundDescription: "No published workflow carries this name.",
  backToWorkflows: "Workflows",
  graphUnavailable: "This revision's graph cannot be drawn here.",
  panelTitle: "Node",
  panelRole: "Role",
  panelPromptStart: "Prompt template",
  panelNoRole: "This node declares no role.",
  panelNoPromptStart: "No prompt excerpt is published for this node.",
  panelClose: "Close node detail"
} as const;

export function workflowFormatFact(formatVersion: 1 | 2 | 3, nodeCount: number | null): string {
  const parts = [`format ${formatVersion}`];
  if (nodeCount !== null) parts.push(`${nodeCount} nodes`);
  return parts.join(" · ");
}
