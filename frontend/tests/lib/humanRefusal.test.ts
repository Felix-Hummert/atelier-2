import { describe, expect, it } from "vitest";

import { CockpitRequestError } from "../../src/api/client";
import {
  cannotBeStarted,
  humanErrorMessage,
  humanProblemDetail,
  humanStartRefusal
} from "../../src/lib/humanRefusal";

const LIVE_ZERO_OUTPUTS =
  "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges";

describe("human start refusals", () => {
  it("turns the live zero-output token into a next action naming the node", () => {
    expect(humanStartRefusal(LIVE_ZERO_OUTPUTS)).toBe(
      "This workflow declares no output on node 'implement'. Add one outputs: entry there and publish again."
    );
    expect(cannotBeStarted(LIVE_ZERO_OUTPUTS)).toContain("Add one outputs: entry");
    expect(cannotBeStarted(LIVE_ZERO_OUTPUTS)).not.toContain("agent-output-shape-unavailable");
  });

  it("turns a too-many-outputs token into a one-output next action", () => {
    expect(
      humanStartRefusal(
        "agent-output-shape-unavailable: 2 outputs on node 'review', and an agent node completes with the one value its own schema judges"
      )
    ).toContain("Keep one outputs: entry");
  });

  it("leaves an unknown token raw", () => {
    expect(humanStartRefusal("brand-new-refusal: something invented")).toBe(
      "brand-new-refusal: something invented"
    );
  });
});

describe("human problem details", () => {
  it("turns durable-state-corrupt into a next action and leaves an unknown code raw", () => {
    expect(
      humanProblemDetail({
        type: "urn:atelier2:problem:v1:durable-state-corrupt",
        detail: "Stop mutation and inspect the durable store."
      })
    ).toBe(
      "The workshop cannot read this stored work. Refresh the page; if it stays, an operator must inspect the durable store."
    );
    expect(
      humanProblemDetail({
        type: "urn:atelier2:problem:v1:temporarily-unavailable",
        detail: "Retry later."
      })
    ).toBe("Retry later.");
  });

  it("reads a CockpitRequestError through its problem, not its raw message", () => {
    const error = new CockpitRequestError("Stop mutation and inspect the durable store.", {
      type: "urn:atelier2:problem:v1:durable-state-corrupt",
      title: "Durable state is corrupt",
      status: 500,
      detail: "Stop mutation and inspect the durable store."
    });
    expect(humanErrorMessage(error, "fallback")).toContain("Refresh the page");
    expect(humanErrorMessage(error, "fallback")).not.toContain("Stop mutation");
  });

  it("names a round trip that never happened by the caller's own sentence, not the browser's raw transport text (#700)", () => {
    const error = new CockpitRequestError("Failed to fetch", null, false, true);
    expect(humanErrorMessage(error, "Workflows are unavailable right now.")).toBe(
      "Workflows are unavailable right now."
    );
  });

  it("keeps a contract violation's own specific message even without a server-answered problem", () => {
    const error = new CockpitRequestError("The workflow response did not match the requested revision.");
    expect(humanErrorMessage(error, "fallback")).toBe(
      "The workflow response did not match the requested revision."
    );
  });
});
