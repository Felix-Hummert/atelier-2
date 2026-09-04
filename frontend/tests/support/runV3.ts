import type {
  DefectiveRunRow,
  RunCancellability,
  RunListRow,
  RunNotCancellableReason,
  RunV3,
  WorkflowRevisionDetail
} from "../../src/api/client";

/**
 * The one V3 scenario the room and listing tests share: a four-node line
 * agent -> action -> wait -> final, one run walking it, and the format-3
 * frames that move it. Anything a single test alone cares about stays in
 * that test.
 */

export const revisionHash = "a".repeat(64);
export const publicReference = "run1.cnVu";
export const workflowName = "Four steps in a line";

export function eventCursor(sequence: number): string {
  return `event1.cnVu.${sequence}`;
}

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

export function workflowRevision(): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    provenance: null,
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 4,
      agent_roles: ["builder"],
      orders: [],
      wait_answer_schemas: [
        {
          node_id: "wait",
          schema: { ref: "answer.schema.json", revision: revisionHash },
          kind: "free",
          string_typed: false,
          values: null
        }
      ],
      node_previews: [
        { id: "agent", kind: "agent", role: "builder", instruction_start: "Build it", depends_on: [] },
        { id: "action", kind: "action", role: null, instruction_start: null, depends_on: ["agent"] },
        { id: "wait", kind: "wait", role: null, instruction_start: null, depends_on: ["action"] },
        { id: "final", kind: "subworkflow", role: null, instruction_start: null, depends_on: ["wait"] }
      ],
      loops: [],
      name: workflowName,
      description: null
    }
  };
}

type RailState = RunV3["node_rail"][number]["state"];

function rail(
  states: [RailState, RailState, RailState, RailState]
): RunV3["node_rail"] {
  const nodeIds = ["agent", "action", "wait", "final"] as const;
  return nodeIds.map((nodeId, index) => ({
    node_id: nodeId,
    state: states[index]!,
    attempt: null
  }));
}

/** STARTED at the Agent node, before any durable event exists. */
export function startedRun(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "run",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    workflow_name: workflowName,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    work_item_reference: null,
    answer: null,
    refusal_output: null,
    state_version: 0,
    state: "STARTED",
    current_node_id: "agent",
    current_node_execution_id: revisionHash,
    node_rail: rail(["working", "queued", "queued", "queued"]),
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: null,
    ended_at: null,
    ...changes
  };
}

/** WAITING_INPUT at the Wait node: the operator owns the next move. */
export function waitingInputRun(changes: Partial<RunV3> = {}): RunV3 {
  return startedRun({
    state_version: 3,
    state: "WAITING_INPUT",
    current_node_id: "wait",
    node_rail: rail(["succeeded", "succeeded", "needs_you", "queued"]),
    cancellation: notCancellableBlock("waiting-for-you"),
    latest_event_cursor: eventCursor(3),
    ...changes
  });
}

/** WAITING_RECONCILIATION at the Action node: the operator owns the decision. */
export function waitingReconciliationRun(changes: Partial<RunV3> = {}): RunV3 {
  return startedRun({
    state_version: 2,
    state: "WAITING_RECONCILIATION",
    current_node_id: "action",
    node_rail: rail(["succeeded", "needs_you", "queued", "queued"]),
    cancellation: notCancellableBlock("waiting-for-you"),
    latest_event_cursor: eventCursor(2),
    ...changes
  });
}

/** COMPLETED at the terminal node: nothing waits for anybody. */
export function completedRun(changes: Partial<RunV3> = {}): RunV3 {
  return startedRun({
    state_version: 5,
    state: "COMPLETED",
    current_node_id: "final",
    node_rail: rail(["succeeded", "succeeded", "succeeded", "succeeded"]),
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: revisionHash,
    latest_event_cursor: eventCursor(5),
    ...changes
  });
}

/** A healthy row on a run list page, wrapping any of the run builders above. */
export function runRow(run: RunV3): RunListRow {
  return { kind: "run", run };
}

/** A listed run whose own projection failed (#1042), the other row shape. */
export function defectiveRunRow(changes: Partial<DefectiveRunRow> = {}): DefectiveRunRow {
  return {
    kind: "defective",
    public_run_reference: publicReference,
    problem_code: "durable-state-corrupt",
    // The production bound (`bounded_run_row_defect_detail`) can only ever
    // narrow a defective row's detail to an exception class name, never a
    // sentence -- the default here stays honest to that shape.
    detail: "RunTransitionConflict",
    ...changes
  };
}

type EventIdentity = Partial<{
  cursor: string;
  public_run_reference: string;
  workflow_revision_hash: string;
  node_id: string;
}>;

/** The format-3 pause frame the wait scenario emits, rail and all. */
export function waitingInput(sequence: number, changes: EventIdentity = {}) {
  return {
    workflow_format_version: 3 as const,
    cursor: eventCursor(sequence),
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    node_id: "wait",
    node_execution_id: revisionHash,
    event_hash: revisionHash,
    node_rail: rail(["succeeded", "succeeded", "needs_you", "queued"]),
    event: "WAITING_INPUT" as const,
    ...changes
  };
}
