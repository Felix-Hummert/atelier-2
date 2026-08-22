import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  NODE_STATES,
  PUBLIC_ATTEMPT_STATES,
  agentConfigurationRevisionPageSchema,
  authProfileRevisionPageSchema,
  workflowDeclaredOrderSchema,
  workflowDeclaredSchemaSchema,
  workflowNodePreviewSchema,
  workflowRevisionDetailSchema,
  catalogNameResolutionSchema,
  occupancyRevisionSchema,
  projectListSchema,
  projectResourceSchema,
  workflowRevisionSummarySchema
} from "../../src/api/client";

/**
 * The frozen OpenAPI document is the one object both sides can read: the server
 * renders it from its own vocabulary owner, and the checked-in artefact only
 * changes when someone decides a wire change. Reading it here is what makes
 * "the browser knows the states the server serves" a test instead of a habit.
 */
const servedDocument = JSON.parse(
  readFileSync(resolve(process.cwd(), "..", "tests", "api", "openapi_frozen.json"), "utf8")
) as {
  components: {
    schemas: Record<
      string,
      { enum?: string[]; properties?: Record<string, { enum?: string[] }> }
    >;
  };
};

describe("the served vocabulary", () => {
  it("proves(the-browser-and-the-served-contract-know-the-same-node-states): the browser decodes exactly the node states the document serves", () => {
    expect([...NODE_STATES]).toEqual(
      servedDocument.components.schemas.NodeRailResource?.properties?.state?.enum
    );
  });

  it("decodes exactly the agent attempt states the document serves", () => {
    expect([...PUBLIC_ATTEMPT_STATES]).toEqual(
      servedDocument.components.schemas.AgentAttemptResourceV2?.properties?.state?.enum
    );
  });

  /**
   * This one exists because it was missing. The described listing was built
   * server-side while this decoder still refused its fields, and every frontend
   * test mocked the call away, so nothing red until the page threw in a browser.
   * Comparing the decoder's own keys against the document's makes a wire
   * enrichment fail here instead of on the operator's screen.
   */
  it("decodes exactly the fields the described revision listing serves", () => {
    const served = servedDocument.components.schemas.WorkflowRevisionSummaryResourceV2;

    expect(Object.keys(workflowRevisionSummarySchema.shape).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
  });

  it("accepts a described revision built from the document's own field set", () => {
    const served = servedDocument.components.schemas.WorkflowRevisionSummaryResourceV2;
    const sample: Record<string, unknown> = {
      workflow_revision_hash: "a".repeat(64),
      workflow_format_version: 3,
      executable: false,
      not_executable_reason: "agent forms nothing binds yet: outputs",
      name: "Implement a candidate, then review it for defects",
      description: null
    };

    expect(Object.keys(sample).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
    expect(workflowRevisionSummarySchema.parse(sample)).toEqual(sample);
  });

  it("bounds the node-preview instruction start to the length the document serves", () => {
    const served = servedDocument.components.schemas.WorkflowNodePreviewResourceV3;
    const instruction = (
      served?.properties as {
        instruction_start?: { anyOf?: Array<{ maxLength?: number }> };
      } | undefined
    )?.instruction_start;
    const maxLength = instruction?.anyOf?.find((option) => option.maxLength !== undefined)
      ?.maxLength;

    expect(maxLength).toBe(120);
    expect(
      workflowNodePreviewSchema.parse({
        id: "implement",
        kind: "agent",
        role: "builder",
        instruction_start: "ä".repeat(maxLength ?? 0),
        depends_on: []
      }).instruction_start
    ).toHaveLength(maxLength ?? 0);
    expect(() =>
      workflowNodePreviewSchema.parse({
        id: "implement",
        kind: "agent",
        role: "builder",
        instruction_start: "ä".repeat((maxLength ?? 0) + 1),
        depends_on: []
      })
    ).toThrow();
  });

  it("decodes exactly the fields the published V3 graph serves", () => {
    const served = servedDocument.components.schemas.WorkflowGraphResourceV3;
    const sample = {
      workflow_revision_hash: "a".repeat(64),
      document_base64: "YQ==",
      graph: {
        workflow_format_version: 3 as const,
        executable: true,
        not_executable_reason: null,
        node_count: 1,
        agent_roles: ["cook"],
        orders: [
          {
            name: "portions",
            schema: {
              ref: "portions-schema",
              revision: "schema-portions"
            }
          }
        ],
        node_previews: [
          {
            id: "cook",
            kind: "agent" as const,
            role: "cook",
            instruction_start: "Cook exactly what the order says.",
            depends_on: []
          }
        ],
        loops: [],
        name: "Cook to order",
        description: null
      }
    };

    expect(Object.keys(sample.graph).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
    expect(Object.keys(workflowNodePreviewSchema.shape).sort()).toEqual(
      Object.keys(
        servedDocument.components.schemas.WorkflowNodePreviewResourceV3?.properties ?? {}
      ).sort()
    );
    expect(Object.keys(workflowDeclaredOrderSchema.shape).sort()).toEqual(
      Object.keys(
        servedDocument.components.schemas.WorkflowDeclaredOrderResourceV3?.properties ?? {}
      ).sort()
    );
    expect(Object.keys(workflowDeclaredSchemaSchema.shape).sort()).toEqual(
      Object.keys(
        servedDocument.components.schemas.WorkflowDeclaredSchemaResourceV3?.properties ??
          {}
      ).sort()
    );
    expect(workflowRevisionDetailSchema.parse(sample)).toEqual(sample);
  });

  it("decodes exactly the fields the catalog name resolution serves", () => {
    const served = servedDocument.components.schemas.CatalogNameResolutionResource;

    expect(Object.keys(catalogNameResolutionSchema.shape).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
  });

  it("decodes exactly the zero-or-one project collection the server serves", () => {
    const resource = servedDocument.components.schemas.ProjectResource;
    const collection = servedDocument.components.schemas.ProjectListResource as {
      properties?: { items?: { maxItems?: number } };
    };
    const publicReference = servedDocument.components.schemas.PublicProjectReference as {
      maxLength: number;
      pattern: string;
    };
    const longestReference = `project1.${"A".repeat(
      publicReference.maxLength - "project1.".length
    )}`;

    expect(Object.keys(projectResourceSchema.shape).sort()).toEqual(
      Object.keys(resource?.properties ?? {}).sort()
    );
    expect(Object.keys(projectListSchema.shape).sort()).toEqual(
      Object.keys(collection.properties ?? {}).sort()
    );
    expect(collection.properties?.items?.maxItems).toBe(1);
    expect(new RegExp(publicReference.pattern).test(longestReference)).toBe(true);
    expect(
      projectResourceSchema.safeParse({ public_project_reference: longestReference }).success
    ).toBe(true);
    expect(
      projectResourceSchema.safeParse({ public_project_reference: `${longestReference}A` })
        .success
    ).toBe(false);
    expect(
      projectResourceSchema.safeParse({ public_project_reference: "project1.@@" }).success
    ).toBe(false);
    expect(
      projectListSchema.parse({
        items: [{ public_project_reference: "project1.dGVhbS9yZWQ" }]
      })
    ).toEqual({ items: [{ public_project_reference: "project1.dGVhbS9yZWQ" }] });
  });

  it("decodes exactly the project occupancy resource the server serves", () => {
    const resource = servedDocument.components.schemas.OccupancyRevisionResource;

    expect(Object.keys(occupancyRevisionSchema.shape).sort()).toEqual(
      Object.keys(resource?.properties ?? {}).sort()
    );
  });

  it("decodes exactly the fields the agent-configuration listing serves", () => {
    const served = servedDocument.components.schemas.AgentConfigurationRevisionPageResource;

    expect(Object.keys(agentConfigurationRevisionPageSchema.shape).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
  });

  it("decodes only the closed startability pair on a listed configuration", () => {
    const sample = {
      model: "sonnet",
      auth_profile_revision_hash: "a".repeat(64),
      executor_revision: "claude-subscription/v1",
      provider_id: "anthropic",
      auth_mode: "subscription" as const,
      requested_capability: "headless" as const,
      agent_configuration_revision_hash: "b".repeat(64),
      startable: false,
      not_startable_reason: "agent-executor-binding-unavailable" as const
    };

    expect(
      agentConfigurationRevisionPageSchema.parse({
        items: [sample],
        next_after_revision_hash: null
      }).items
    ).toEqual([sample]);
    expect(() =>
      agentConfigurationRevisionPageSchema.parse({
        items: [{ ...sample, startable: true }],
        next_after_revision_hash: null
      })
    ).toThrow();
  });

  it("decodes exactly the fields the auth-profile listing serves", () => {
    const served = servedDocument.components.schemas.AuthProfileRevisionPageResource;

    expect(Object.keys(authProfileRevisionPageSchema.shape).sort()).toEqual(
      Object.keys(served?.properties ?? {}).sort()
    );
  });
});
