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
});
