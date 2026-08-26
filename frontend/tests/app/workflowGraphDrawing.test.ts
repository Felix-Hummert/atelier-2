import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it } from "vitest";

import WorkflowGraphDrawing from "../../src/components/WorkflowGraphDrawing.svelte";
import { stateGlyphs } from "../../src/components/StateMark.svelte";

const chain = [
  {
    id: "review",
    kind: "agent" as const,
    role: "builder",
    instruction_start: "Check what the node before you did.",
    depends_on: ["implement"]
  },
  {
    id: "implement",
    kind: "agent" as const,
    role: "builder",
    instruction_start: "Do the one thing this chain is for.",
    depends_on: []
  }
];

afterEach(() => cleanup());

describe("the V3 graph drawing", () => {
  it("places nodes in deterministic layers even when the excerpt arrives reversed", () => {
    render(WorkflowGraphDrawing, { props: { previews: chain } });

    const graph = screen.getByRole("region", { name: "Workflow" });
    const implement = graph.querySelector('[data-node-id="implement"]');
    const review = graph.querySelector('[data-node-id="review"]');

    expect(implement?.getAttribute("data-layer")).toBe("0");
    expect(review?.getAttribute("data-layer")).toBe("1");
    expect(graph.querySelector('[data-layer="0"] [data-node-id="implement"]')).not.toBeNull();
    expect(graph.querySelector('[data-layer="1"] [data-node-id="review"]')).not.toBeNull();
  });

  it("carries each node's state in its name and in its mark, never in colour alone", () => {
    render(WorkflowGraphDrawing, {
      props: {
        previews: chain,
        rail: [
          { node_id: "implement", state: "succeeded" },
          { node_id: "review", state: "working" }
        ],
        currentNodeId: "review",
        onSelect: () => undefined
      }
    });

    const graph = screen.getByRole("region", { name: "Workflow" });
    const implement = within(graph).getByRole("button", { name: "implement — Done" });
    const review = within(graph).getByRole("button", { name: "review — Working" });

    expect(implement.getAttribute("data-state")).toBe("succeeded");
    expect(implement.textContent).toContain(stateGlyphs.succeeded);
    expect(review.getAttribute("data-state")).toBe("working");
    expect(review.textContent).toContain(stateGlyphs.working);
    expect(review.classList.contains("current")).toBe(true);
    expect(review.getAttribute("data-live")).toBe("true");
    expect(implement.getAttribute("data-live")).toBeNull();
    // The running node breathes through the graph's own round pulse ring
    // (keyed on data-live), never the rail's rectangular breathing glow: the
    // global "live-work" class would have drawn a box-shadow whose right edge
    // fell into the empty gap between nodes and read as a stray vertical bar
    // (#598).
    expect(review.classList.contains("live-work")).toBe(false);
  });

  it("carries each node's kind in its shape, and shows no type token beside its name", () => {
    render(WorkflowGraphDrawing, {
      props: {
        previews: [
          { id: "gate", kind: "wait" as const, role: null, instruction_start: null, depends_on: [] },
          { id: "open pr", kind: "action" as const, role: null, instruction_start: null, depends_on: ["gate"] }
        ],
        onSelect: () => undefined
      }
    });

    const graph = screen.getByRole("region", { name: "Workflow" });

    expect(graph.querySelector('[data-node-id="gate"]')?.getAttribute("data-node-kind")).toBe(
      "wait"
    );
    expect(graph.querySelector('[data-node-id="open pr"]')?.getAttribute("data-node-kind")).toBe(
      "action"
    );
    // "WAIT gate" tells a person nothing they can act on (operator, 23.08.):
    // the shape carries the kind, the legend explains the shape.
    expect(graph.textContent).not.toContain("wait");
  });

  it("still draws a single node that names no edge", () => {
    render(WorkflowGraphDrawing, {
      props: {
        previews: [
          {
            id: "only",
            kind: "agent" as const,
            role: "builder",
            instruction_start: "Do the one thing.",
            depends_on: []
          }
        ]
      }
    });

    const graph = screen.getByRole("region", { name: "Workflow" });
    expect(graph.querySelector('[data-node-id="only"]')?.getAttribute("data-layer")).toBe("0");
    expect(within(graph).getByText("only").isConnected).toBe(true);
  });

  it("draws a dashed box around a declared loop's body, naming its round bound", () => {
    render(WorkflowGraphDrawing, {
      props: {
        previews: chain,
        loops: [
          {
            id: "until_reviewed",
            member_node_ids: ["implement", "review"],
            maximum_rounds: 3,
            repeat_while: null
          }
        ]
      }
    });

    const graph = screen.getByRole("region", { name: "Workflow" });
    const box = within(graph).getByRole("group", { name: "↻ max 3" });

    expect(within(box).getByText("implement").isConnected).toBe(true);
    expect(within(box).getByText("review").isConnected).toBe(true);
  });

  it("names the earlier verdict exit beside the round bound when the document declares one", () => {
    render(WorkflowGraphDrawing, {
      props: {
        previews: chain,
        loops: [
          {
            id: "until_reviewed",
            member_node_ids: ["implement", "review"],
            maximum_rounds: 3,
            repeat_while: { node: "review", verdict: "revise" }
          }
        ]
      }
    });

    const graph = screen.getByRole("region", { name: "Workflow" });

    expect(within(graph).getByRole("group", { name: "↻ until revise · max 3" })).toBeTruthy();
  });

  it("draws no loop box when the document declares no loop", () => {
    render(WorkflowGraphDrawing, { props: { previews: chain } });

    const graph = screen.getByRole("region", { name: "Workflow" });

    expect(within(graph).queryByRole("group")).toBeNull();
  });

  it("leaves a node outside every box once its layer mixes an unrelated node", () => {
    render(WorkflowGraphDrawing, {
      props: {
        previews: [
          ...chain,
          {
            id: "unrelated",
            kind: "agent" as const,
            role: "builder",
            instruction_start: "An entry node the loop does not repeat.",
            depends_on: []
          }
        ],
        loops: [
          {
            id: "until_reviewed",
            member_node_ids: ["implement", "review"],
            maximum_rounds: 3,
            repeat_while: null
          }
        ]
      }
    });

    const graph = screen.getByRole("region", { name: "Workflow" });

    expect(within(graph).queryByRole("group")).toBeNull();
    expect(within(graph).getByText("unrelated").isConnected).toBe(true);
  });
});
