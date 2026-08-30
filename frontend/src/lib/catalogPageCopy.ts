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
  oneFile: "1 file",

  all: "All",
  workflowsTitle: "Workflows",
  workflowsLabel: "workflows",
  agentsTitle: "Agents",
  agentsLabel: "agents",
  agentsByProvider: "Agents by provider",
  skillsTitle: "Skills",
  catalogGroups: "Catalog groups",
  search: "Search…",
  searchLabel: "Search the catalog",
  catalogEmpty: "Drop a workflow, an agent, or a plugin folder — anywhere on this page.",
  workflowsUnavailable: "Workflows unavailable",
  workflowsIncomplete: "Workflows incomplete",

  agentsUnavailable: "Agents unavailable",
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
  kind: "Kind",
  kindWorkflow: "Workflow",
  kindAgent: "Agent",
  addToCatalog: "Add to catalog",
  addingToCatalog: "Adding…",
  noKindDeclared: "no kind declared yet",
  importFailed: "This file could not be imported.",
  recognitionFailed: "This file could not be recognized.",
  notAWorkflow: "This is not a workflow — nothing was added.",
  notAnAgent: "This is not an agent — nothing was added."
} as const;

/**
 * The detail behind one catalog entry: its still graph, the node panel, and
 * what the room says when the name is gone. Same owner as the list, because
 * the detail is this room's own page (ADR 0019 §1), not a room of its own.
 */
export const workflowDetailCopy = {
  detailUnavailable: "Workflow detail unavailable",
  detailLabel: "workflow detail",
  technical: "Technical",
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
  retiredNotice: "This workflow's catalog lineage was retired. Starting it is not offered here.",
  retire: "Retire",
  retireTitle: (name: string) => `Retire ${name}?`,
  retireDisappears: "Leaves",
  retireDisappearsFact: "It leaves Catalog and can no longer be started.",
  retireStays: "History",
  retireStaysFact: "Past runs and immutable revisions remain reachable.",
  retirePermanent: "Permanent",
  retirePermanentFact: "This workflow cannot return to Catalog. There is no way back.",
  retireFailed: "This workflow could not be retired."
} as const;

/** Copy owned by the catalog detail's one manual-start sheet. */
export const workflowStartCopy = {
  startTitle: (name: string) => `Start ${name}`,
  configurationFor: (role: string) => `Configuration for ${role}`,
  preparing: "Preparing…",
  sheetUnavailable: "The start sheet could not be prepared.",
  workItem: "Work item",
  noSource: "No source",
  connectSource: "Connect one in Settings",
  unknownSource: "Other source",
  orderUnavailable: "This order shape cannot be started here.",
  roles: "Roles",
  choose: "Choose",
  chosenNow: "Chosen now",
  pinnedInWorkflow: "pinned in workflow",
  nextHigher: "next higher",
  unavailable: "Unavailable",
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
  startUnavailable: "The run could not be started.",
  github: "GitHub",
  gitlab: "GitLab",
  configurationsIncomplete: "Agent configurations are incomplete.",
  accountsIncomplete: "Accounts are incomplete.",
  servedProjectMissing: "Served project missing.",
  rolesUnresolved: "Model resolution did not name exactly these roles.",
  observedQueueIncomplete: "Observed queue items are incomplete.",
  startResponseChangedRoles: "The start response changed the selected roles.",
  startResponseUnproven: "The start response did not prove the exact request."
} as const;

export function startConfigurationLabel(
  providerId: string,
  modelId: string,
  accountId: string
): string {
  return `${providerId} · ${modelId} · Account ${accountId}`;
}

export function startAccountSuffix(accountId: string): string {
  return ` · Account ${accountId}`;
}

export function startUnavailableSuffix(): string {
  return ` · ◇ ${workflowStartCopy.unavailable}`;
}

export function pinnedModelLine(model: string, account: string, unavailable: string): string {
  return `${workflowStartCopy.pinnedInWorkflow} → ${model}${account}${unavailable}`;
}

export function projectDefaultLine(
  difficulty: number,
  model: string,
  nextHigher: boolean,
  account: string,
  unavailable: string
): string {
  const fallback = nextHigher ? ` (${workflowStartCopy.nextHigher})` : "";
  return `difficulty ${difficulty} → ${model}${fallback}${account}${unavailable}`;
}

export function startOrderGroup(name: string): string {
  return `Order ${name}`;
}

export function workItemFor(orderName: string): string {
  return `${workflowStartCopy.workItem} for ${orderName}`;
}

export function observedSourceHeading(projectId: string, platform: string): string {
  return `${projectId} · ${platform}`;
}

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
