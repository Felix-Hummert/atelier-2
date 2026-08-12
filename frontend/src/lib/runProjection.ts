import { decodeRunEvent, type Problem, type RunEvent } from "../api/client";

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
  rawData: string
): StreamProjection {
  let decoded: RunEvent;
  try {
    decoded = decodeRunEvent(JSON.parse(rawData));
  } catch {
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

function bytesEqual(left: Uint8Array, right: Uint8Array): boolean {
  if (left.byteLength !== right.byteLength) {
    return false;
  }
  return left.every((value, index) => value === right[index]);
}
