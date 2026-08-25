/**
 * Copy the Catalog room renders: what this atelier can run, and where it came
 * from.
 *
 * One owner per screen, the convention `workflowsPageCopy` and `railCopy`
 * already hold to, so `?pseudo-locale=1` (`wrapDisplayCopy`) proves every
 * string on this surface has a source.
 *
 * The words are the operator's, not the store's: a person recognises
 * "Available workflows", never "published revisions of kind workflow".
 */
export const catalogPageCopy = {
  title: "Catalog",
  // The literal room sentence (operator ruling #684): the catalog is the
  // library, everything published, seen with its provenance -- never the
  // door that starts a run, which is Workflows' sentence, not this room's.
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
  admitted: "Admitted — you can start it by name now.",
  // A workflow entry links into the start room, never the reverse (operator
  // ruling #684): this room shows what is published, Workflows starts it.
  start: "Start",

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
