import type { CockpitApi, RunV3, WorkflowRevisionDetail } from "../api/client";
import { problemCode } from "./catalogName";
import { readEveryAgentConfiguration } from "./runPages";

const CONDUCTOR_WORKFLOW_NAME = "conductor";

export interface ConductorConnection {
  workflowRevisionHash: string;
  role: string;
  agentConfigurationRevisionHash: string;
  maximumRounds: number;
}

/**
 * The published loop a Workbench conversation can operate. Its first node is
 * the input-bearing wait, so the start door has no graph inputs to invent.
 */
export async function resolveConductorConnection(
  cockpitApi: CockpitApi
): Promise<ConductorConnection | null> {
  let workflowRevisionHash: string;
  try {
    const resolution = await cockpitApi.getRevisionByName(CONDUCTOR_WORKFLOW_NAME);
    workflowRevisionHash = resolution.workflow_revision_hash;
  } catch (error) {
    const code = problemCode(error);
    if (code === "catalog-name-not-found" || code === "catalog-lineage-retired") {
      return null;
    }
    throw error;
  }

  const revision = await cockpitApi.getWorkflowRevision(workflowRevisionHash);
  const shape = conductorConversationShape(revision);
  if (shape === null) return null;

  const projects = await cockpitApi.listProjects();
  const project = projects.items[0];
  if (project === undefined) return null;
  const resolution = await cockpitApi.resolveProjectModels(
    project.public_project_reference,
    workflowRevisionHash,
    []
  );
  const boundConfigurationHash = resolution.resolutions.find(
    (binding) => binding.role === shape.role
  )?.agent_configuration_revision_hash;
  if (boundConfigurationHash === undefined || boundConfigurationHash === null) return null;

  const reading = await readEveryAgentConfiguration((after) =>
    cockpitApi.listAgentConfigurationRevisions(after)
  );
  const bound = reading.configurations.find(
    (configuration) =>
      configuration.agent_configuration_revision_hash === boundConfigurationHash
  );
  if (bound === undefined || !bound.startable) return null;
  return {
    workflowRevisionHash,
    role: shape.role,
    agentConfigurationRevisionHash: boundConfigurationHash,
    maximumRounds: shape.maximumRounds
  };
}

export function conductorConversationShape(
  revision: WorkflowRevisionDetail
): { role: string; maximumRounds: number } | null {
  const graph = revision.graph;
  if (!graph.executable || graph.orders.length !== 0) return null;
  const [role, ...moreRoles] = graph.agent_roles;
  const [loop, ...moreLoops] = graph.loops;
  if (role === undefined || moreRoles.length > 0 || loop === undefined || moreLoops.length > 0) {
    return null;
  }
  const members = new Set(loop.member_node_ids);
  const waits = graph.node_previews.filter((node) => members.has(node.id) && node.kind === "wait");
  const agents = graph.node_previews.filter((node) => members.has(node.id) && node.kind === "agent");
  const [waitAnswer, ...moreWaitAnswers] = graph.wait_answer_schemas.filter(
    (answer) => answer.node_id === "next_message"
  );
  return loop.repeat_while === null &&
    waits.length === 1 &&
    agents.length === 1 &&
    waits[0]?.id === "next_message" &&
    agents[0]?.id === "conduct" &&
    agents[0]?.role === role &&
    // The conductor's own fixed message schema (`CONDUCTOR_MESSAGE_SCHEMA`,
    // `host/conductor_workflow.py`) is `{type: "string", minLength: 1}`, so
    // its wait classifies "string" (#1091), not "free".
    waitAnswer?.kind === "string" &&
    moreWaitAnswers.length === 0
    ? { role, maximumRounds: loop.maximum_rounds }
    : null;
}

/** The one live conductor conversation is the newest started, non-terminal run. */
export function newestConductorConversation(
  runs: readonly RunV3[],
  workflowRevisionHashes: ReadonlySet<string> | string
): RunV3 | null {
  const belongsToConductor = (workflowRevisionHash: string): boolean =>
    typeof workflowRevisionHashes === "string"
      ? workflowRevisionHash === workflowRevisionHashes
      : workflowRevisionHashes.has(workflowRevisionHash);
  const candidates = runs.filter(
    (run) =>
      belongsToConductor(run.workflow_revision_hash) &&
      (run.state === "STARTED" || run.state === "WAITING_INPUT" || run.state === "WAITING_RECONCILIATION") &&
      hasStartedStamp(run)
  );
  return candidates.reduce<RunV3 | null>((newest, run) => {
    if (newest === null) return run;
    const newestStamp = newest.started_at as string;
    const runStamp = run.started_at as string;
    return runStamp > newestStamp ||
      (runStamp === newestStamp && run.public_run_reference > newest.public_run_reference)
      ? run
      : newest;
  }, null);
}

function hasStartedStamp(run: RunV3): run is RunV3 & { started_at: string } {
  return typeof run.started_at === "string" && Number.isFinite(Date.parse(run.started_at));
}
