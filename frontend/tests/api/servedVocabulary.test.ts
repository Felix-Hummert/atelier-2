import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { NODE_STATES, PUBLIC_ATTEMPT_STATES } from "../../src/api/client";

/**
 * The frozen OpenAPI document is the one object both sides can read: the server
 * renders it from its own vocabulary owner, and the checked-in artefact only
 * changes when someone decides a wire change. Reading it here is what makes
 * "the browser knows the states the server serves" a test instead of a habit.
 */
const servedDocument = JSON.parse(
  readFileSync(resolve(process.cwd(), "..", "tests", "api", "openapi_frozen.json"), "utf8")
) as { components: { schemas: Record<string, { enum?: string[]; properties?: Record<string, { enum?: string[] }> }> } };

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
});
