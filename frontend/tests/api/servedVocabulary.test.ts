import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  NODE_STATES,
  PUBLIC_ATTEMPT_STATES,
  workflowNodePreviewSchema,
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
      revision_hash: "a".repeat(64),
      format_version: 3,
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
        instruction_start: "ä".repeat(maxLength ?? 0)
      }).instruction_start
    ).toHaveLength(maxLength ?? 0);
    expect(() =>
      workflowNodePreviewSchema.parse({
        id: "implement",
        kind: "agent",
        role: "builder",
        instruction_start: "ä".repeat((maxLength ?? 0) + 1)
      })
    ).toThrow();
  });
});
