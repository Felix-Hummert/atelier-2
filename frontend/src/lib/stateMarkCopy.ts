import type { NodeState } from "./runProjection";

/**
 * What the operator reads for a state the server named. One owner, one word,
 * shared by the mark, the graph, the rail, and the node panel.
 */
export const stateLabels: Record<NodeState, string> = {
  queued: "Queued",
  working: "Working",
  needs_you: "Needs you",
  succeeded: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  interrupted: "Interrupted"
};

export function nodeAriaName(nodeId: string, state: NodeState): string {
  return `${nodeId} — ${stateLabels[state]}`;
}
