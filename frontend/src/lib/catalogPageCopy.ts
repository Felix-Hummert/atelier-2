import type { CatalogNameState } from "./catalogName";

/**
 * Copy the Catalog room renders: what this atelier can run, and where it came
 * from.
 *
 * One owner per room, the convention `workbenchPageCopy` and `railCopy`
 * already hold to, so `?pseudo-locale=1` (`wrapDisplayCopy`) proves every
 * string on this surface has a source.
 *
 * The words are the operator's, not the store's: a person recognises
 * "Available workflows", never "published revisions of kind workflow".
 */
export const catalogPageCopy = {
  title: "Catalog",
  import: "Import",
  filePicker: "Catalog file picker",
  oneWorkflow: "1 workflow",
  oneAgent: "1 agent",

  all: "All",
  workflowsTitle: "Workflows",
  agentsTitle: "Agents",
  skillsTitle: "Skills",
  search: "Search…",
  searchLabel: "Search the catalog",
  catalogEmpty: "Drop a workflow, an agent, or a plugin folder — anywhere on this page.",
  workflowsUnavailable: "Workflows unavailable",
  workflowsIncomplete: "Workflows incomplete",

  agentsUnavailable: "Agents unavailable",
  agentsIncomplete: "Agents incomplete",

  skillsNone: "A plugin folder brings skills.",

  provenanceManual: "Manual import",
  // The provider an imported agent belongs to. The import door takes exactly
  // one authoring format — the Markdown definition with frontmatter, which is
  // Claude's — so the format the bytes arrived in is what names the provider.
  // A door for another format (`.toml` for Codex) names its own.
  agentProviderClaude: "Claude",
  noDescription: "No description.",
  // The same words a run header already uses for a document that declares no
  // name, so one thing is called one thing across the workshop.
  unnamedWorkflow: "Unnamed workflow",

  newerRevisionHint: "A newer published revision is available.",
  notAdmittedHint: "This published workflow is not in the catalog yet.",
  newerRevision: "Newer revision",
  notAdmitted: "Not in catalog",
  notExecutable: "Not executable",

  // The catalog detail owns the only manual start door (ADR 0019 §1).
  start: "Start",

  cancel: "Cancel",
  close: "Close",
  addToCatalog: "Add to catalog",
  addingToCatalog: "Adding…",
  importFailed: "This file could not be imported.",
  recognitionFailed: "This file could not be recognized.",
  unrecognized: "This doesn't look like a workflow, an agent, or a plugin — nothing was added.",
  fileUnreadable: "That file could not be read."
} as const;

/**
 * The detail behind one catalog entry: its still graph, the node panel, and
 * what the room says when the name is gone. Same owner as the list, because
 * the detail is this room's own page (ADR 0019 §1), not a room of its own.
 */
export const workflowDetailCopy = {
  detailUnavailable: "Workflow detail unavailable",
  notFoundTitle: "Workflow not found",
  notFoundDescription: "No published workflow carries this name.",
  graphUnavailable: "This revision's graph cannot be drawn here.",
  workflowRevision: "Workflow revision",
  sealsWorkflowRevision: "the published workflow revision",
  orders: "Orders",
  noOrders: "No orders declared.",
  schema: "Schema",
  schemaUnavailable: "Schema summary unavailable.",
  schemaAcceptsAny: "Any JSON value.",
  required: "required",
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

/** Copy owned by the catalog detail's one manual-start sheet. */
export const workflowStartCopy = {
  preparing: "Preparing…",
  sheetUnavailable: "The start sheet could not be prepared.",
  noConfiguration: "No executable configuration is available.",
  workItem: "Work item",
  noSource: "No source",
  settings: "Settings",
  unknownSource: "Other source",
  orderUnavailable: "This order shape cannot be started here.",
  roles: "Roles",
  choose: "Choose",
  chosenNow: "Chosen now",
  pinnedInWorkflow: "pinned in workflow",
  nextHigher: "next higher",
  unavailable: "Unavailable",
  missingRoleResolution: "Model resolution did not name this role.",
  overrideNotRegistered: "The chosen configuration is not registered.",
  workflowModelNotRegistered: "The model pinned in the workflow is not registered.",
  workflowModelAmbiguous: "The model pinned in the workflow names more than one configuration.",
  noProjectDefault: "No project default answers this difficulty or a higher one.",
  familyDifferenceUnavailable: "No registered configuration satisfies the workflow's family rule.",
  familyDifferenceFrom: "No registered configuration differs in family from",
  trueLabel: "True",
  falseLabel: "False",
  startRun: "Start run",
  startNeedsWorkItem: "Choose a work item before starting.",
  startNeedsWorkItemSource: "Connect a source in Settings before starting.",
  startNeedsOrder: "Complete each required order before starting.",
  startPreparing: "Preparing the start options.",
  startNeedsConfiguration: (role: string) => `Choose a configuration for ${role} before starting.`,
  tryAgain: "Try again",
  cancel: "Cancel",
  retry: "Retry",
  startUnavailable: "The run could not be started."
} as const;

/**
 * The short state a card or detail header wears beside a name that is not
 * the catalog's current head for it -- `null` for the ordinary case, an
 * admitted head, which wears no note at all.
 *
 * Read-only browsing shows every published name it can identify rather than
 * hiding one, unlike a project configuration picker (a write
 * precondition: it must bind to a live catalog member, so it drops what
 * cannot be bound). This surface only answers "what can the house do", so an
 * honest note beats disappearing content -- the same choice the saved-workflow
 * picker on the start door already makes for the same three states.
 */
export function catalogStateNote(state: CatalogNameState | undefined): string | null {
  if (state === undefined || state.kind === "admitted") return null;
  if (state.kind === "retired") return workflowDetailCopy.retiredNote;
  return workflowDetailCopy.notAdmittedNote;
}
