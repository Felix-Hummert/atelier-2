import type { RunCancellability, RunNotCancellableReason } from "../../src/api/client";

/**
 * The server-owned cancellability block a V3 run always carries (#439 P5). One
 * owner for the two honest shapes so fixtures across the suite state a run's
 * cancellability the same way the wire does, and a change to the shape fails in
 * one place.
 */
export function cancellableBlock(
  targetNodeExecutionId = "d".repeat(64)
): RunCancellability {
  return { cancellable: true, reason: null, target_node_execution_id: targetNodeExecutionId };
}

export function notCancellableBlock(reason: RunNotCancellableReason): RunCancellability {
  return { cancellable: false, reason, target_node_execution_id: null };
}
