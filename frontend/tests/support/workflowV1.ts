import { executableGraph } from "../../src/api/client";
import type { RunV1, WorkflowRevisionDetail } from "../../src/api/client";

/**
 * The one V1 scenario the cockpit tests share: a four-node workflow
 * agent -> action -> wait -> final, one run walking it, and the durable events
 * that move it. Anything a single test alone cares about stays in that test.
 */

export const revisionHash = "a".repeat(64);
export const publicReference = "run1.cnVu";
/** SHA-256 of the exact answer bytes "17". */
export const answerHash = "4523540f1504cd17100c4835e85b7eefd49911580f8efff0599a8f283be6b9e3";

export function eventCursor(sequence: number): string {
  return `event1.cnVu.${sequence}`;
}

export function workflowRevision(): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "",
    graph: {
      workflow_format_version: 1,
      start_node_id: "agent",
      nodes: [
        { type: "agent", node_id: "agent", job: "Build it", output: "candidate", next_node_id: "action" },
        { type: "action", node_id: "action", next_node_id: "wait" },
        { type: "wait", node_id: "wait", answer_type: "integer", next_node_id: "final" },
        { type: "subworkflow", node_id: "final", operation: "add", operands: [2, 3], next_node_id: null }
      ]
    }
  };
}

function nodeAt(index: number): RunV1["current_node"] {
  return executableGraph(workflowRevision().graph).nodes[index]! as RunV1["current_node"];
}

/** STARTED at the Agent node, before any durable event exists. */
export function startedRun(changes: Partial<RunV1> = {}): RunV1 {
  return {
    run_id: "run",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    state_version: 0,
    state: "STARTED",
    current_node: nodeAt(0),
    waiting: { type: "NONE" },
    terminal_hash: null,
    latest_event_cursor: null,
    ...changes
  };
}

/** STARTED at the Action node, after the Agent completed. */
export function agentCompletedRun(changes: Partial<RunV1> = {}): RunV1 {
  return startedRun({
    state_version: 1,
    current_node: nodeAt(1),
    latest_event_cursor: eventCursor(1),
    ...changes
  });
}

/** WAITING_INPUT at the Wait node: the operator owns the next move. */
export function waitingInputRun(changes: Partial<RunV1> = {}): RunV1 {
  return startedRun({
    state_version: 3,
    state: "WAITING_INPUT",
    current_node: nodeAt(2),
    waiting: { type: "WAITING_INPUT", node_id: "wait", answer_type: "integer" },
    latest_event_cursor: eventCursor(3),
    ...changes
  });
}

/** WAITING_RECONCILIATION at the Action node: the operator owns the decision. */
export function waitingReconciliationRun(changes: Partial<RunV1> = {}): RunV1 {
  return startedRun({
    state: "WAITING_RECONCILIATION",
    current_node: nodeAt(1),
    waiting: {
      type: "WAITING_RECONCILIATION",
      node_id: "act",
      logical_effect_key: "effect",
      request_hash: revisionHash,
      request_base64: "",
      intent_state_version: 0,
      pending_command: null
    },
    ...changes
  });
}

/** COMPLETED at the terminal node: nothing waits for anybody. */
export function completedRun(changes: Partial<RunV1> = {}): RunV1 {
  return startedRun({
    state: "COMPLETED",
    current_node: nodeAt(3),
    terminal_hash: revisionHash,
    ...changes
  });
}

/** STARTED at the final node, after the Wait answer was durable. */
export function answeredRun(changes: Partial<RunV1> = {}): RunV1 {
  return startedRun({
    state_version: 4,
    current_node: nodeAt(3),
    latest_event_cursor: eventCursor(4),
    ...changes
  });
}

type EventIdentity = Partial<{
  cursor: string;
  public_run_reference: string;
  workflow_revision_hash: string;
  node_id: string;
}>;

function envelope(sequence: number, nodeId: string) {
  return {
    cursor: eventCursor(sequence),
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    node_id: nodeId,
    node_execution_id: revisionHash,
    event_hash: revisionHash
  };
}

export function agentCompleted(sequence: number, changes: EventIdentity & { output?: string } = {}) {
  return {
    ...envelope(sequence, "agent"),
    event: "AGENT_COMPLETED" as const,
    output: "candidate",
    payload_hash: revisionHash,
    ...changes
  };
}

export function actionCompleted(sequence: number, changes: EventIdentity = {}) {
  return {
    ...envelope(sequence, "action"),
    event: "ACTION_COMPLETED" as const,
    receipt: {
      logical_effect_key: "effect",
      request_hash: revisionHash,
      effect_id: "effect-1",
      result_hash: revisionHash,
      result_base64: "cmVzdWx0",
      confirmation_source: "ADAPTER_EXECUTION" as const,
      reconcile_command_id: null
    },
    ...changes
  };
}

export function waitingInput(sequence: number, changes: EventIdentity = {}) {
  return {
    ...envelope(sequence, "wait"),
    event: "WAITING_INPUT" as const,
    answer_type: "integer" as const,
    ...changes
  };
}

export function waitAnswered(
  sequence: number,
  changes: EventIdentity & { answer?: string; answer_hash?: string } = {}
) {
  return {
    ...envelope(sequence, "wait"),
    event: "WAIT_ANSWERED" as const,
    answer: "17",
    answer_hash: answerHash,
    ...changes
  };
}

export function reconciliationRequired(
  sequence: number,
  changes: EventIdentity & { request_hash?: string } = {}
) {
  return {
    ...envelope(sequence, "action"),
    event: "ACTION_RECONCILIATION_REQUIRED" as const,
    request_base64: "cmVxdWVzdA==",
    request_hash: revisionHash,
    ...changes
  };
}
