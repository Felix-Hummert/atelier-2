import type { CatalogNameState } from "./catalogName";

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
  title: "Workflows",
  // The literal room sentence (operator ruling #684): this room admits only
  // startable workflows and every card leads to starting one -- never a
  // browsable library, which is the Catalog's sentence, not this room's.
  lead: "Start a workflow — every admitted, startable workflow this workshop can run right now.",
  emptyTitle: "Nothing is admitted yet",
  emptyDescription: "A workflow becomes startable here once the catalog admits it.",
  emptyNext: "Open the catalog",
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
  panelClose: "Close node detail",
  notAdmittedNote: "Not admitted to the catalog.",
  retiredNote: "Retired",
  retiredNotice: "This workflow's catalog lineage was retired. Starting it is not offered here."
} as const;

/**
 * The short state a card or detail header wears beside a name that is not
 * the catalog's current head for it -- `null` for the ordinary case, an
 * admitted head, which wears no note at all.
 *
 * Read-only browsing shows every published name it can identify rather than
 * hiding one, unlike the project occupancy editor's picker (a write
 * precondition: it must bind to a live catalog member, so it drops what
 * cannot be bound). This surface only answers "what can the house do", so an
 * honest note beats disappearing content -- the same choice the saved-workflow
 * picker on `/atelier/new` already makes for the same three states.
 */
export function catalogStateNote(state: CatalogNameState | undefined): string | null {
  if (state === undefined || state.kind === "admitted") return null;
  if (state.kind === "retired") return workflowsPageCopy.retiredNote;
  return workflowsPageCopy.notAdmittedNote;
}
