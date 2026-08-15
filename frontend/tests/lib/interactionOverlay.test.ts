import { describe, expect, it } from "vitest";
import { applyInteractionOverlay, isStilled } from "../../src/lib/interactionOverlay";
import type { NodeProjection, NodeState } from "../../src/lib/runProjection";

function railEntry(nodeId: string, state: NodeState): NodeProjection {
  return {
    node: { type: "wait", node_id: nodeId, answer_type: "integer", next_node_id: "final" },
    state,
    last_event: null,
    attempt: null
  };
}

function statesOf(rail: readonly NodeProjection[]): NodeState[] {
  return rail.map((entry) => entry.state);
}

describe("the interaction overlay", () => {
  it("proves(the-interaction-overlay-lifts-and-stills-under-separate-gates): lifts a node that needs the operator while his own form is open", () => {
    const rail = [railEntry("wait", "needs_you")];

    expect(statesOf(applyInteractionOverlay(rail, new Set(["wait"])))).toEqual(["working"]);
  });

  it("proves(the-interaction-overlay-lifts-and-stills-under-separate-gates): leaves a node that needs the operator waiting while no form is open", () => {
    const rail = [railEntry("wait", "needs_you")];

    expect(statesOf(applyInteractionOverlay(rail, new Set()))).toEqual(["needs_you"]);
  });

  it.each(["queued", "working", "succeeded", "failed", "cancelled", "interrupted"] as const)(
    "proves(the-interaction-overlay-lifts-and-stills-under-separate-gates): leaves a %s node exactly as the server named it, open form or not",
    (state) => {
      const rail = [railEntry("node", state)];

      expect(statesOf(applyInteractionOverlay(rail, new Set(["node"])))).toEqual([state]);
    }
  );

  it("proves(the-interaction-overlay-lifts-and-stills-under-separate-gates): stills the nodes whose form is open and no other, whatever their state", () => {
    expect(isStilled("action", new Set(["action"]))).toBe(true);
    expect(isStilled("action", new Set(["wait"]))).toBe(false);
    expect(isStilled("action", new Set())).toBe(false);
  });

  it("proves(the-interaction-overlay-lifts-and-stills-under-separate-gates): stills a durably working node it never lifted", () => {
    const openForms = new Set(["action"]);
    const rail = [railEntry("action", "working")];

    expect(statesOf(applyInteractionOverlay(rail, openForms))).toEqual(["working"]);
    expect(isStilled("action", openForms)).toBe(true);
  });
});
