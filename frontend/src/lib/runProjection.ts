import {
  decodeRunEvent,
  type Problem,
  type Run,
  type RunEvent,
  type RunV2,
  type WorkflowGraph,
  type WorkflowNode
} from "../api/client";

export type RequestState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "failed"; problem: Problem };

export interface RetainedResource<T> {
  confirmed: T | null;
  request: RequestState;
}

export type ConnectionState = "connecting" | "live" | "reconnecting" | "complete";
export type ProtocolProblem =
  | { type: "decoder" }
  | { type: "sequence_gap"; expected: number; received: number }
  | { type: "conflicting_duplicate"; cursor: string };

export interface StreamProjection {
  public_run_reference: string;
  workflow_revision_hash: string;
  events: readonly RunEvent[];
  last_sequence: number;
  connection: ConnectionState;
  protocol_problem: ProtocolProblem | null;
  payload_bytes_by_cursor: ReadonlyMap<string, Uint8Array>;
}

export type NodeState =
  | "queued"
  | "working"
  | "needs_you"
  | "done"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface AgentAttemptProjection {
  ordinal: 1 | 2;
  state: RunV2["agent_attempts"][number]["state"] | "COMPLETED";
}

export interface NodeProjection {
  node: WorkflowNode;
  state: NodeState;
  last_event: RunEvent | null;
  attempt: AgentAttemptProjection | null;
}

export function startLoading<T>(resource: RetainedResource<T>): RetainedResource<T> {
  return { confirmed: resource.confirmed, request: { state: "loading" } };
}

export function confirmResource<T>(
  _resource: RetainedResource<T>,
  confirmed: T
): RetainedResource<T> {
  return { confirmed, request: { state: "idle" } };
}

export function failResource<T>(
  resource: RetainedResource<T>,
  problem: Problem
): RetainedResource<T> {
  return { confirmed: resource.confirmed, request: { state: "failed", problem } };
}

export function streamProjection(
  publicRunReference: string,
  workflowRevisionHash: string
): StreamProjection {
  return {
    public_run_reference: publicRunReference,
    workflow_revision_hash: workflowRevisionHash,
    events: [],
    last_sequence: 0,
    connection: "connecting",
    protocol_problem: null,
    payload_bytes_by_cursor: new Map()
  };
}

export function restartStreamProjection(
  projection: StreamProjection,
  publicRunReference: string,
  workflowRevisionHash: string
): StreamProjection {
  if (
    projection.public_run_reference !== publicRunReference ||
    projection.workflow_revision_hash !== workflowRevisionHash
  ) {
    throw new Error("the durable stream identity changed during restart");
  }
  return {
    ...projection,
    connection: "connecting",
    protocol_problem: null
  };
}

export function markConnecting(
  projection: StreamProjection,
  reconnecting = false
): StreamProjection {
  return {
    ...projection,
    connection: reconnecting ? "reconnecting" : "connecting"
  };
}

export function markLive(projection: StreamProjection): StreamProjection {
  return { ...projection, connection: "live" };
}

export function markComplete(projection: StreamProjection): StreamProjection {
  return { ...projection, connection: "complete" };
}

export function decodeAndApplyDurableEvent(
  projection: StreamProjection,
  rawData: string,
  graph?: WorkflowGraph
): StreamProjection {
  let decoded: RunEvent;
  try {
    decoded = decodeRunEvent(JSON.parse(rawData));
  } catch {
    return { ...projection, protocol_problem: { type: "decoder" } };
  }
  if (graph !== undefined && !eventMatchesGraph(decoded, graph)) {
    return { ...projection, protocol_problem: { type: "decoder" } };
  }
  return applyDurableEvent(projection, rawData, decoded);
}

export function applyDurableEvent(
  projection: StreamProjection,
  rawData: string,
  event: RunEvent
): StreamProjection {
  if (projection.protocol_problem !== null) {
    return projection;
  }
  if (
    event.public_run_reference !== projection.public_run_reference ||
    event.workflow_revision_hash !== projection.workflow_revision_hash
  ) {
    return { ...projection, protocol_problem: { type: "decoder" } };
  }
  const payloadBytes = new TextEncoder().encode(rawData);
  if (event.sequence <= projection.last_sequence) {
    const known = projection.payload_bytes_by_cursor.get(event.cursor);
    if (known !== undefined && bytesEqual(known, payloadBytes)) {
      return projection;
    }
    return {
      ...projection,
      protocol_problem: { type: "conflicting_duplicate", cursor: event.cursor }
    };
  }
  const expected = projection.last_sequence + 1;
  if (event.sequence !== expected) {
    return {
      ...projection,
      protocol_problem: { type: "sequence_gap", expected, received: event.sequence }
    };
  }
  const payloads = new Map(projection.payload_bytes_by_cursor);
  payloads.set(event.cursor, payloadBytes);
  return {
    ...projection,
    events: [...projection.events, event],
    last_sequence: event.sequence,
    payload_bytes_by_cursor: payloads
  };
}

export function projectNodeRail(
  run: Run,
  graph: WorkflowGraph,
  events: readonly RunEvent[],
  workingHumanNodeIds: ReadonlySet<string> = new Set()
): readonly NodeProjection[] {
  const nodes = orderedNodes(graph);
  const currentIndex = nodes.findIndex((node) => node.node_id === run.current_node.node_id);
  if (currentIndex < 0) {
    throw new Error("the durable run current node is absent from its workflow revision");
  }
  const latestEvent = events.at(-1) ?? null;
  const eventsLeadSnapshot =
    latestEvent !== null && latestEvent.sequence > snapshotEventSequence(run);
  return nodes.map((node, index) => {
    const lastEvent = lastNodeEvent(events, node.node_id);
    const durableState = eventsLeadSnapshot
      ? eventDrivenNodeState(node, lastEvent, latestEvent, nodes)
      : snapshotNodeState(run, node, index, currentIndex, lastEvent);
    return {
      node,
      state:
        durableState === "needs_you" && workingHumanNodeIds.has(node.node_id)
          ? "working"
          : durableState,
      last_event: lastEvent,
      attempt: projectAgentAttempt(run, node, lastEvent, eventsLeadSnapshot)
    };
  });
}

function orderedNodes(graph: WorkflowGraph): WorkflowNode[] {
  const byId = new Map(graph.nodes.map((node) => [node.node_id, node]));
  const ordered: WorkflowNode[] = [];
  const visited = new Set<string>();
  let nextId: string | null = graph.start_node_id;
  while (nextId !== null) {
    if (visited.has(nextId)) {
      throw new Error("the workflow graph contains a cycle");
    }
    const node = byId.get(nextId);
    if (node === undefined) {
      throw new Error("the workflow graph references a missing node");
    }
    ordered.push(node);
    visited.add(nextId);
    nextId = node.next_node_id;
  }
  if (ordered.length !== graph.nodes.length) {
    throw new Error("the workflow graph contains unreachable nodes");
  }
  return ordered;
}

function lastNodeEvent(events: readonly RunEvent[], nodeId: string): RunEvent | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event?.node_id === nodeId) return event;
  }
  return null;
}

function snapshotNodeState(
  run: Run,
  node: WorkflowNode,
  index: number,
  currentIndex: number,
  lastEvent: RunEvent | null
): NodeState {
  if (
    index === currentIndex &&
    "workflow_format_version" in run &&
    node.type === "agent"
  ) {
    const attemptState = run.agent_attempts.at(-1)?.state;
    if (attemptState === "FAILED") return "failed";
    if (attemptState === "CANCELLED") return "cancelled";
    if (attemptState === "INTERRUPTED") return "interrupted";
    if (attemptState !== undefined) return "working";
  }
  if (lastEvent !== null) {
    const terminal = terminalNodeState(lastEvent);
    if (terminal !== null) return terminal;
  }
  if (index < currentIndex) {
    return "done";
  }
  if (index > currentIndex) {
    return "queued";
  }
  if (run.state === "COMPLETED") {
    return "done";
  }
  if (run.state === "WAITING_INPUT") {
    return "needs_you";
  }
  if (run.state === "WAITING_RECONCILIATION") {
    return run.waiting.type === "WAITING_RECONCILIATION" &&
      run.waiting.pending_command !== null
      ? "working"
      : "needs_you";
  }
  return node.node_id === run.current_node.node_id ? "working" : "queued";
}

function projectAgentAttempt(
  run: Run,
  node: WorkflowNode,
  event: RunEvent | null,
  eventsLeadSnapshot: boolean
): AgentAttemptProjection | null {
  if (!("workflow_format_version" in run) || node.type !== "agent") return null;
  const eventAttempt =
    event !== null && "workflow_format_version" in event && "attempt_id" in event
      ? { ordinal: event.attempt_ordinal, state: agentEventStates[event.event] }
      : null;
  const currentAttempt =
    node.node_id === run.current_node.node_id ? run.agent_attempts.at(-1) ?? null : null;
  if (
    eventsLeadSnapshot &&
    eventAttempt !== null &&
    (currentAttempt === null || eventAttempt.ordinal >= currentAttempt.attempt_ordinal)
  ) {
    return eventAttempt;
  }
  return currentAttempt === null
    ? eventAttempt
    : { ordinal: currentAttempt.attempt_ordinal, state: currentAttempt.state };
}

const agentEventStates = {
  AGENT_COMPLETED: "COMPLETED",
  AGENT_FAILED: "FAILED",
  AGENT_CANCEL_REQUESTED: "CANCEL_REQUESTED",
  AGENT_CANCELLED: "CANCELLED",
  AGENT_INTERRUPTED: "INTERRUPTED"
} as const;

function eventDrivenNodeState(
  node: WorkflowNode,
  lastNodeEventValue: RunEvent | null,
  latestEvent: RunEvent,
  nodes: readonly WorkflowNode[]
): NodeState {
  if (lastNodeEventValue !== null) {
    const terminal = terminalNodeState(lastNodeEventValue);
    if (terminal !== null) return terminal;
  }
  if (latestEvent.node_id === node.node_id) {
    if (
      latestEvent.event === "ACTION_RECONCILIATION_REQUIRED" ||
      latestEvent.event === "WAITING_INPUT"
    ) {
      return "needs_you";
    }
    return "working";
  }
  if (isTerminalNodeEvent(latestEvent)) {
    const completedNode = nodes.find((candidate) => candidate.node_id === latestEvent.node_id);
    if (completedNode?.next_node_id === node.node_id) {
      return "working";
    }
  }
  return "queued";
}

function snapshotEventSequence(run: Run): number {
  if (run.latest_event_cursor === null) return 0;
  const encoded = run.latest_event_cursor.split(".").at(-1);
  if (encoded === undefined) {
    throw new Error("the durable run event cursor is incomplete");
  }
  const sequence = Number(encoded);
  if (!Number.isSafeInteger(sequence) || sequence <= 0) {
    throw new Error("the durable run event cursor has an invalid sequence");
  }
  return sequence;
}

function isTerminalNodeEvent(event: RunEvent): boolean {
  return (
    event.event === "AGENT_COMPLETED" ||
    event.event === "ACTION_COMPLETED" ||
    event.event === "WAIT_ANSWERED" ||
    event.event === "SUBWORKFLOW_COMPLETED"
  );
}

function terminalNodeState(event: RunEvent): NodeState | null {
  if (event.event === "AGENT_FAILED") return "failed";
  if (event.event === "AGENT_CANCELLED") return "cancelled";
  if (event.event === "AGENT_INTERRUPTED") return "interrupted";
  if (event.event === "AGENT_COMPLETED" && "workflow_format_version" in event) {
    return "completed";
  }
  return isTerminalNodeEvent(event) ? "done" : null;
}

function eventMatchesGraph(event: RunEvent, graph: WorkflowGraph): boolean {
  const eventFormat = "workflow_format_version" in event ? 2 : 1;
  if (eventFormat !== graph.format_version) return false;
  const node = graph.nodes.find((candidate) => candidate.node_id === event.node_id);
  if (node === undefined) return false;
  if (node.type === "agent") {
    return [
      "AGENT_COMPLETED",
      "AGENT_FAILED",
      "AGENT_CANCEL_REQUESTED",
      "AGENT_CANCELLED",
      "AGENT_INTERRUPTED"
    ].includes(event.event);
  }
  if (node.type === "action") {
    return (
      event.event === "ACTION_RECONCILIATION_REQUIRED" ||
      event.event === "ACTION_RECONCILIATION_RESOLVED" ||
      event.event === "ACTION_COMPLETED"
    );
  }
  if (node.type === "wait") {
    return event.event === "WAITING_INPUT" || event.event === "WAIT_ANSWERED";
  }
  return event.event === "SUBWORKFLOW_COMPLETED";
}

function bytesEqual(left: Uint8Array, right: Uint8Array): boolean {
  if (left.byteLength !== right.byteLength) {
    return false;
  }
  return left.every((value, index) => value === right[index]);
}
