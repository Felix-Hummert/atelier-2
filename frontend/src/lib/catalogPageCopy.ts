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
  // The room sentence (ADR 0019 §1): the catalog is the library and the one
  // door that starts a piece by hand -- what the house can do, from where, and
  // how to get more. There is no second room that starts a run.
  lead: "What the workshop has — every published workflow, agent, and skill, each with its provenance.",

  workflowsTitle: "Available workflows",
  workflowsEmpty: "Nothing published yet — import a workflow file below.",
  workflowsUnavailable: "Workflows unavailable",
  workflowsIncomplete: "Workflows incomplete",

  // An imported agent is provider-bound and passed through whole; the atelier
  // translates nothing. Neutrality lives in a workflow's role and its casting,
  // never in the file, so the heading says which axis this list is sorted on.
  agentsTitle: "Available agents (by provider)",
  agentsEmpty: "No agent published yet — import an .agent.md file below.",
  // A published definition is not yet something any executor runs: it ends at
  // the agent configuration, and nothing binds a run to it. The row says so
  // rather than wearing a state that would read as "ready".
  agentPublishedOnly: "Published — no executor runs it yet",
  agentsUnavailable: "Agents unavailable",
  agentsIncomplete: "Agents incomplete",

  skillsTitle: "Available skills",
  // Honest, not apologetic: there is no publishing door for a skill in this
  // build, and #660's Git link is the one that will bring them.
  skillsNone: "Skills arrive with the Git link this atelier does not hold yet.",

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

  startable: "Startable",
  notAdmitted: "Not in the catalog yet",
  notExecutable: "Not executable",
  // The live duplicate-card finding's fix (#659, sharpened by #684): a
  // published sibling of an admitted name shows here, under that one card,
  // instead of wearing a second card with the same name.
  newerRevisionAvailable: "Newer revision available",

  admit: "Admit into catalog",
  admitting: "Admitting…",
  admitFailed: "This workflow could not be admitted.",
  admitted: "Admitted — you can open it by name now.",
  // The catalog detail owns the only manual start door (ADR 0019 §1).
  start: "Start",
  // The one door into the workflow detail page (#695): a named revision's
  // node-graph preview and per-node facts, which this entry itself does not
  // carry. Shown for any named revision -- admitted, not yet admitted, or not
  // executable -- since seeing the graph and why a node refuses to run is
  // exactly what this door is for.
  details: "Details",

  importWorkflowTitle: "Import a workflow",
  importWorkflowHint: "Choose a .yaml file, or paste the exact document.",
  importWorkflowLabel: "Exact workflow YAML",
  importWorkflowFailed: "This workflow could not be imported.",

  importAgentTitle: "Import an agent",
  importAgentHint: "Choose an .agent.md file, or paste the exact document.",
  importAgentLabel: "Exact agent definition",
  importAgentFailed: "This agent could not be imported.",

  chooseFile: "Choose a file",
  importing: "Importing…",
  importAction: "Import",
  imported: "Imported.",
  emptyDocument: "There is nothing to import yet — choose a file or paste a document.",
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
  interim: "Interim",
  info: "Info",
  interimConfigurationInfo: "Interim configuration",
  interimConfiguration: "This choice applies to this run only, until Settings › Model defaults exist.",
  interimConfigurationNeeded: "Interim source · choose for this run",
  interimConfigurationChosen: "Interim source · chosen for this run, not saved",
  choose: "Choose",
  trueLabel: "True",
  falseLabel: "False",
  startRun: "Start run",
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
 * hiding one, unlike the project occupancy editor's picker (a write
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
