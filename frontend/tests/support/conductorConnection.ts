import { vi } from "vitest";

import type { CockpitApi, WorkflowRevisionDetail } from "../../src/api/client";

/**
 * A connected conductor's own conversation, real reducer and journal
 * included: the shared fixture below is the smallest published loop
 * (`resolveConductorConnection`'s own shape check) any scenario needs to
 * actually connect a conductor.
 */
export const conductorRevisionHash = "9".repeat(64);
export const conductorConfigurationHash = "8".repeat(64);
export const conductorRole = "conductor";
export const conductorProjectReference = "project1";

export function conductorRevisionDetail(): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: conductorRevisionHash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 2,
      agent_roles: [conductorRole],
      orders: [],
      wait_answer_schemas: [
        {
          node_id: "next_message",
          schema: { ref: "message", revision: conductorRevisionHash },
          kind: "string",
          string_typed: true,
          values: null
        }
      ],
      node_previews: [
        { id: "next_message", kind: "wait", role: null, instruction_start: null, depends_on: [] },
        {
          id: "conduct",
          kind: "agent",
          role: conductorRole,
          instruction_start: "Answer the operator",
          depends_on: ["next_message"]
        }
      ],
      loops: [
        {
          id: "conversation",
          member_node_ids: ["next_message", "conduct"],
          maximum_rounds: 3,
          repeat_while: null
        }
      ],
      name: "Conductor",
      description: null
    }
  } as WorkflowRevisionDetail;
}

/** Every read `resolveConductorConnection` (conductorEpisode.ts) makes to bind one live conductor. */
export function conductorConnectionOverrides(): Partial<CockpitApi> {
  return {
    getRevisionByName: vi.fn(async () => ({
      display_name: "conductor",
      lineage_id: "7".repeat(64),
      catalog_revision_hash: conductorRevisionHash,
      revision_number: 1
    })),
    getWorkflowRevision: vi.fn(async () => conductorRevisionDetail()),
    listProjects: vi.fn(async () => ({
      items: [{ public_project_reference: conductorProjectReference }]
    })),
    resolveProjectModels: vi.fn(async () => ({
      project_id: "conductor-project",
      public_project_reference: conductorProjectReference,
      workflow_revision_hash: conductorRevisionHash,
      resolutions: [
        {
          role: conductorRole,
          agent_configuration_revision_hash: conductorConfigurationHash,
          source: "chosen-now" as const,
          model_id: "conductor-model",
          declared_difficulty: 1 as const,
          default_difficulty: null,
          uncast_reason: null,
          family_differs_from: null
        }
      ]
    })),
    listAgentConfigurationRevisions: vi.fn(async () => ({
      items: [
        {
          agent_configuration_revision_hash: conductorConfigurationHash,
          provider_id: "test",
          model: "conductor-model",
          auth_mode: "subscription" as const,
          auth_profile_revision_hash: "6".repeat(64),
          executor_revision: "immediate/v1",
          requested_capability: "headless" as const,
          startable: true,
          structurally_startable: true,
          not_startable_reason: null,
          provider_probe_problem_code: null,
          provider_probe_observed_at: null
        }
      ],
      next_after_revision_hash: null
    }))
  };
}
