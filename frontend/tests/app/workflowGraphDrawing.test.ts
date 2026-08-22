import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it } from "vitest";

import WorkflowGraphDrawing from "../../src/components/WorkflowGraphDrawing.svelte";

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
    render(WorkflowGraphDrawing, { props: { previews: chain, showExcerpt: true } });

    const graph = screen.getByRole("region", { name: "Workflow" });
    const implement = graph.querySelector('[data-node-id="implement"]');
    const review = graph.querySelector('[data-node-id="review"]');

    expect(implement?.getAttribute("data-layer")).toBe("0");
    expect(review?.getAttribute("data-layer")).toBe("1");
    expect(graph.querySelector('[data-layer="0"] [data-node-id="implement"]')).not.toBeNull();
    expect(graph.querySelector('[data-layer="1"] [data-node-id="review"]')).not.toBeNull();
  });

  it("paints each node's state from the rail by shape and by name", () => {
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

    expect(implement.querySelector(".state-succeeded")).not.toBeNull();
    expect(implement.querySelector(".state-shape")?.textContent).toContain("✓");
    expect(review.querySelector(".state-working")).not.toBeNull();
    expect(review.querySelector(".state-shape")?.textContent).toContain("▲");
    expect(review.classList.contains("current")).toBe(true);
    expect(review.classList.contains("live-work")).toBe(true);
    expect(review.getAttribute("data-live")).toBe("true");
    expect(implement.classList.contains("live-work")).toBe(false);
    expect(implement.getAttribute("data-live")).toBeNull();
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

  it("names the loop marker in the legend beside the node shapes", () => {
    render(WorkflowGraphDrawing, { props: { previews: chain, showLegend: true } });

    const legend = screen.getByRole("list", { name: "Node shapes and the loop marker" });

    expect(within(legend).getByText("Loop").isConnected).toBe(true);
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
