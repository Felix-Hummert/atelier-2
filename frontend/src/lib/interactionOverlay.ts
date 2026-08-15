import type { NodeProjection } from "./runProjection";

/**
 * The one state rule this browser owns, and the reason it is not on the wire:
 * which form the operator has open is a fact about this window, not about the
 * run. Two windows may honestly disagree, so it is applied after the served
 * rail — never inside the wire decoding — and never sent back.
 *
 * It says one thing, "the operator is at this node", through two rules gated
 * separately: lifting also asks the node, stilling asks the window alone, and
 * so it holds for a node that is working without ever having waited. `isStilled`
 * is not given the state, so re-coupling them would have to change a signature.
 */
export function applyInteractionOverlay(
  rail: readonly NodeProjection[],
  openFormNodeIds: ReadonlySet<string>
): readonly NodeProjection[] {
  return rail.map((entry) =>
    entry.state === "needs_you" && openFormNodeIds.has(entry.node.node_id)
      ? { ...entry, state: "working" }
      : entry
  );
}

export function isStilled(nodeId: string, openFormNodeIds: ReadonlySet<string>): boolean {
  return openFormNodeIds.has(nodeId);
}
