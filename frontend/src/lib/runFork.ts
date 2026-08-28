import type { RunV3 } from "../api/client";
import { runHasEnded } from "./runState";

/**
 * What retry-from-node will carry and what it will run again.
 *
 * The served fork body names only the restart node. The prefix is the rail
 * strictly before that node — the same linear cut slice 1 persists — and the
 * recorded inputs are the order *names* the list already publishes. Order
 * bytes never travel on that resource and must not be reconstructed here.
 */
export type ForkPlan =
  | { readonly kind: "running" }
  | { readonly kind: "unknown-node" }
  | { readonly kind: "prefix-not-reusable" }
  | {
      readonly kind: "ok";
      readonly restartFrom: string;
      readonly carriedNodeIds: readonly string[];
      readonly rerunNodeIds: readonly string[];
      readonly orderNames: readonly string[];
    };

export function planRunFork(run: RunV3, restartFromNodeId: string): ForkPlan {
  if (!runHasEnded(run.state)) {
    return { kind: "running" };
  }
  const restartAt = run.node_rail.findIndex((entry) => entry.node_id === restartFromNodeId);
  if (restartAt < 0) {
    return { kind: "unknown-node" };
  }
  const prefix = run.node_rail.slice(0, restartAt);
  if (prefix.some((entry) => entry.state !== "succeeded")) {
    return { kind: "prefix-not-reusable" };
  }
  return {
    kind: "ok",
    restartFrom: restartFromNodeId,
    carriedNodeIds: prefix.map((entry) => entry.node_id),
    rerunNodeIds: run.node_rail.slice(restartAt).map((entry) => entry.node_id),
    orderNames: run.orders.map((order) => order.name)
  };
}

/** Rail ids or order names, joined the way the run page already lists facts. */
export function forkFactList(names: readonly string[]): string {
  return names.join(" · ");
}
