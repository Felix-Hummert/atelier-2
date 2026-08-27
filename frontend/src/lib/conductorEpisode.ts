import { z } from "zod";

import {
  decodeStreamFrame,
  isStreamFailure,
  type CockpitApi,
  type RunEvent,
  type WorkflowRevisionDetail
} from "../api/client";
import {
  currentChatTranscript,
  markConductorRun,
  settleConductorLine,
  takeConductorTurn,
  type ChatMessage
} from "./chatTranscript";
import { problemCode } from "./catalogName";
import { conductorChatCopy } from "./conductorChatCopy";
import { decodeUtf8Base64 } from "./exactBytes";
import { createRunId, startMutationV3 } from "./mutationJournal";
import { readEveryAgentConfiguration } from "./runPages";

/**
 * The wire between the Workbench composer and one episodic conductor run
 * (issue #7): a sent message becomes ONE run of the published `conductor`
 * workflow through the same public start door every client uses, and the
 * episode's JSON report returns to the conversation as the reply. No private
 * channel exists — everything here has its manual counterpart on the run
 * pages (Keine-Sonderautorität ruling, #7).
 *
 * The names and shapes below mirror the conductor's own contract, owned by
 * `src/atelier2/host/conductor_workflow.py`; the e2e proof drives both sides
 * against one served instance so a drift fails visibly.
 */
const CONDUCTOR_WORKFLOW_NAME = "conductor";
const OPERATOR_SPEAKER = "operator";
const CONDUCTOR_SPEAKER = "conductor";

/**
 * An inline run order is refused above `MAXIMUM_INSTANCE_DOCUMENT_BYTES`
 * (owner: `atelier2.contracts.schemas_v3`), so the brief truncates its prior
 * transcript oldest-first to fit under this mirrored ceiling — and says how
 * many messages it dropped in `dropped_oldest_messages`, so an episode never
 * mistakes a truncated conversation for a whole one.
 */
export const MAXIMUM_BRIEF_BYTES = 16_384;

/** The episode's report, the shape `CONDUCTOR_REPORT_SCHEMA` enforces. */
const conductorReportSchema = z
  .object({
    answer: z.string().min(1),
    started_run_ids: z.array(z.string().min(1))
  })
  .strict();

export interface ConductorConnection {
  workflowRevisionHash: string;
  role: string;
  briefOrderName: string;
  agentConfigurationRevisionHash: string;
}

/**
 * Whether this deployment has a conductor the composer can hand a message to.
 *
 * Connected means: the catalog resolves the `conductor` name to an executable
 * one-agent revision, the served project's model defaults bind that
 * role (#557), and
 * the bound configuration is startable here — which is false exactly where
 * the serve did not arm a doors-capable executor. Every read is the public
 * API the cockpit already uses. Only the named not-connected answers return
 * null; any other failure stays loud with the caller.
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
  const episode = episodeShapeOf(revision);
  if (episode === null) return null;

  const projects = await cockpitApi.listProjects();
  const project = projects.items[0];
  if (project === undefined) return null;
  const resolution = await cockpitApi.resolveProjectModels(
    project.public_project_reference,
    workflowRevisionHash,
    []
  );
  const boundConfigurationHash = resolution.resolutions.find(
    (binding) => binding.role === episode.role
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
    role: episode.role,
    briefOrderName: episode.briefOrderName,
    agentConfigurationRevisionHash: boundConfigurationHash
  };
}

/**
 * The one-agent-one-order shape a conductor episode needs, read from the
 * served revision itself so no role or order name is spelled here twice.
 */
function episodeShapeOf(
  revision: WorkflowRevisionDetail
): { role: string; briefOrderName: string } | null {
  const graph = revision.graph;
  if (graph.workflow_format_version !== 3 || !graph.executable) return null;
  const [role, ...moreRoles] = graph.agent_roles;
  const [order, ...moreOrders] = graph.orders;
  if (role === undefined || moreRoles.length > 0) return null;
  if (order === undefined || moreOrders.length > 0) return null;
  return { role, briefOrderName: order.name };
}

/** The brief's JSON value, and how many oldest messages the ceiling dropped. */
export function conductorBrief(
  prior: readonly ChatMessage[],
  message: string
): { value: string; droppedOldestMessages: number } {
  const spoken = prior
    .filter((line) => line.pending !== true)
    .map((line) => ({
      speaker: line.speaker === "you" ? OPERATOR_SPEAKER : CONDUCTOR_SPEAKER,
      text: line.text
    }));
  let dropped = 0;
  for (;;) {
    const value = JSON.stringify({
      message,
      prior_transcript: spoken.slice(dropped),
      dropped_oldest_messages: dropped
    });
    if (new TextEncoder().encode(value).length <= MAXIMUM_BRIEF_BYTES || dropped >= spoken.length) {
      return { value, droppedOldestMessages: dropped };
    }
    dropped += 1;
  }
}

/**
 * One sent message, end to end: open the turn in the conversation, start one
 * conductor run with the bounded brief as its typed input, follow that run's
 * own event stream, and settle the pending line with the report's answer — or
 * with the honest failure. Fire-and-forget by design: the conversation module
 * outlives the page, so a reply lands even after in-app navigation.
 */
export function sendConductorMessage(
  cockpitApi: CockpitApi,
  connection: ConductorConnection,
  typed: string
): void {
  const prior = currentChatTranscript();
  const pendingId = takeConductorTurn(typed, conductorChatCopy.reading);
  if (pendingId === null) return;
  const brief = conductorBrief(prior, typed.trim());
  void startEpisode(cockpitApi, connection, pendingId, brief.value);
}

async function startEpisode(
  cockpitApi: CockpitApi,
  connection: ConductorConnection,
  pendingId: string,
  briefValue: string
): Promise<void> {
  const mutation = startMutationV3(
    createRunId(),
    connection.workflowRevisionHash,
    [
      {
        role: connection.role,
        agent_configuration_revision_hash: connection.agentConfigurationRevisionHash
      }
    ],
    [{ name: connection.briefOrderName, value: briefValue }]
  );
  let publicRunReference: string;
  try {
    const started = await cockpitApi.start(mutation);
    publicRunReference = started.value.public_run_reference;
  } catch (error) {
    settleConductorLine(
      pendingId,
      `${conductorChatCopy.startRefused} ${failureText(error)}`
    );
    return;
  }
  markConductorRun(pendingId, publicRunReference);
  followEpisode(cockpitApi, publicRunReference, pendingId);
}

/**
 * The reply travels back through the run's existing durable event stream —
 * the same one the run cockpit renders, no second mechanism. The episode is a
 * one-agent run, so its `AGENT_COMPLETED` output IS the schema-validated
 * report, and `AGENT_FAILED` is the episode's honest end. The stream is a
 * comfort view; the run page remains the proof.
 */
function followEpisode(
  cockpitApi: CockpitApi,
  publicRunReference: string,
  pendingId: string
): void {
  const subscription = cockpitApi.openRunEvents(publicRunReference, {
    opened: () => {},
    event: (rawData) => {
      const settled = settleFromEvent(rawData, pendingId);
      if (settled) subscription.close();
    },
    // The browser's EventSource reconnects on its own; the pending line keeps
    // saying the conductor is reading, which stays true.
    disconnected: () => {}
  });
}

function settleFromEvent(rawData: string, pendingId: string): boolean {
  let event: RunEvent;
  try {
    const frame = decodeStreamFrame(JSON.parse(rawData));
    if (isStreamFailure(frame)) {
      // The stream broke, not the episode: the line says so, and its run link
      // stays the honest way to the reply.
      settleConductorLine(pendingId, conductorChatCopy.streamLost);
      return true;
    }
    event = frame;
  } catch {
    settleConductorLine(pendingId, conductorChatCopy.replyUnreadable);
    return true;
  }
  if (event.event === "AGENT_COMPLETED" && "output_base64" in event) {
    settleConductorLine(pendingId, replyText(event.output_base64));
    return true;
  }
  if (event.event === "AGENT_FAILED") {
    settleConductorLine(pendingId, conductorChatCopy.episodeFailed);
    return true;
  }
  return false;
}

function replyText(outputBase64: string): string {
  const decoded = decodeUtf8Base64(outputBase64);
  if (decoded === null) return conductorChatCopy.replyUnreadable;
  let report: z.infer<typeof conductorReportSchema>;
  try {
    report = conductorReportSchema.parse(JSON.parse(decoded));
  } catch {
    return conductorChatCopy.replyUnreadable;
  }
  if (report.started_run_ids.length === 0) return report.answer;
  return `${report.answer}\n${conductorChatCopy.startedRuns} ${report.started_run_ids.join(", ")}`;
}

function failureText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
