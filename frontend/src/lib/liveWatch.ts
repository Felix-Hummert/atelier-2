import type { AnyRun, RunEvent } from "../api/client";
import type { NodeState } from "./runProjection";

/**
 * Whether this page should show live work rather than a finished result.
 *
 * STARTED is the run-level fact. A rail entry in `working` is the node-level
 * fact the operator can see while the run is still moving, including after a
 * later refresh that has not yet changed the run state.
 */
export function runShowsLiveWork(run: AnyRun): boolean {
  if (run.state === "STARTED") return true;
  if ("node_rail" in run) {
    return run.node_rail.some((entry) => entry.state === "working");
  }
  return false;
}

export function workingNodeId(run: AnyRun): string | null {
  if ("node_rail" in run) {
    const working = run.node_rail.find((entry) => entry.state === "working");
    if (working !== undefined) return working.node_id;
  }
  if (run.state !== "STARTED") return null;
  return "current_node" in run ? run.current_node.node_id : run.current_node_id;
}

export function nodeIsLiveWork(state: NodeState | undefined): boolean {
  return state === "working";
}

/** The stream event as a ticker line: kind and node, never the payload. */
export function eventTickerLabel(event: RunEvent): string {
  return event.event.replaceAll("_", " ");
}
