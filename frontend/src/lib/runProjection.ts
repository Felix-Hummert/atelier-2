import {
  decodeCanonicalBase64,
  decodeStreamFrame,
  isRunProjectionCorrupt,
  isStreamFailure,
  type Problem,
  type RunEvent,
  type RunV3,
  type StreamFrame
} from "../api/client";
import { sha256Hex } from "./exactBytes";
import { ageLabel, exactLocal } from "./when";

/**
 * The exact facts a run or a node carries about when it ran, plain data with
 * no copy of its own -- the caller owns the words each fact stands beside.
 *
 * A missing timestamp answers null rather than a placeholder: `endedAt` is
 * absent for a run or node still going, and `startedAt` is absent wherever
 * the store never recorded one (operator ruling 23.08.: an "exact time"
 * reveal that only ever repeated the relative age it sat beside is chrome,
 * not a second fact -- this is the fact itself, always visible).
 */
export interface WhenFacts {
  startedExact: string | null;
  endedExact: string | null;
  durationWords: string | null;
}

export function whenFacts(
  startedAt: string | null,
  endedAt: string | null,
  now: Date
): WhenFacts {
  return {
    startedExact: startedAt === null ? null : exactLocal(startedAt),
    endedExact: endedAt === null ? null : exactLocal(endedAt),
    durationWords:
      startedAt === null
        ? null
        : ageLabel(startedAt, now, "duration", endedAt ?? undefined)
  };
}

/** One field of a declared object result, in the order the answer wrote it. */
export interface ReadableResultField {
  readonly label: string;
  readonly value: string;
}

/**
 * What a node wrote, turned into prose rather than left as a wire shape
 * (#716). The declared profile a run's own schema admits at the top level is
 * always an object or an array (`schemas_v3.declared_instance_in_answer`'s
 * own comment on `_JSON_DOCUMENT_OPENERS`), so those are the two shapes this
 * reads, plus the plain, non-JSON text a free-form schema's answer already is:
 *
 * - an object carrying its own `answer` field reads as that one sentence --
 *   the shape every agent node's report already writes when it means to
 *   speak to a person, the conductor's included -- with every other
 *   *non-empty* field of the same object shown after it, named and valued,
 *   so nothing material hides only in the disclosure;
 * - an object with no such field reads as all of its own fields instead;
 * - an array reads as its items, each read the same way a field's value is;
 * - anything else (prose, or a value no declared shape admits) reads as
 *   itself.
 *
 * `raw` is the exact bytes behind an "Exact text" disclosure, kept only
 * where the readable form is a narrower view of them -- plain text has
 * nothing behind it worth a second copy.
 */
export type ReadableResult =
  | { readonly kind: "text"; readonly text: string; readonly raw: string | null }
  | {
      readonly kind: "object";
      readonly sentence: string | null;
      readonly fields: readonly ReadableResultField[];
      readonly raw: string;
    }
  | { readonly kind: "items"; readonly items: readonly string[]; readonly raw: string };

const DECLARED_ANSWER_SENTENCE_FIELD = "answer";

export function readableResult(decodedAnswer: string): ReadableResult {
  const declared = parseDeclaredValue(decodedAnswer);
  if (declared === null) {
    return { kind: "text", text: decodedAnswer, raw: null };
  }
  if (Array.isArray(declared)) {
    const items = declared.map(readableFieldValue);
    return items.length === 0
      ? { kind: "text", text: decodedAnswer, raw: null }
      : { kind: "items", items, raw: decodedAnswer };
  }
  const sentenceValue = declared[DECLARED_ANSWER_SENTENCE_FIELD];
  const sentence =
    typeof sentenceValue === "string" && sentenceValue.length > 0 ? sentenceValue : null;
  const entries =
    sentence === null
      ? Object.entries(declared)
      : Object.entries(declared).filter(
          ([label, value]) => label !== DECLARED_ANSWER_SENTENCE_FIELD && !isEmptyValue(value)
        );
  const fields = entries.map(([label, value]) => ({ label, value: readableFieldValue(value) }));
  if (sentence === null && fields.length === 0) {
    return { kind: "text", text: decodedAnswer, raw: null };
  }
  return { kind: "object", sentence, fields, raw: decodedAnswer };
}

function parseDeclaredValue(text: string): Record<string, unknown> | unknown[] | null {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (Array.isArray(value)) return value;
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

function isEmptyValue(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.length === 0;
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

function readableFieldValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(readableFieldValue).join(", ");
  return JSON.stringify(value);
}

export type ConnectionState =
  | "connecting"
  | "live"
  | "reconnecting"
  | "complete"
  | "failed";
export type ProtocolProblem =
  | { type: "decoder" }
  | { type: "sequence_gap"; expected: number; received: number }
  | { type: "conflicting_duplicate"; cursor: string }
  | { type: "output_integrity"; cursor: string; expected: string; received: string };

export type AgentOutputProjection =
  | { kind: "utf8"; value: string; byte_count: number }
  | { kind: "binary"; value: string; byte_count: number }
  | { kind: "empty"; value: ""; byte_count: 0 };

export interface StreamProjection {
  public_run_reference: string;
  workflow_revision_hash: string;
  events: readonly RunEvent[];
  last_sequence: number;
  connection: ConnectionState;
  protocol_problem: ProtocolProblem | null;
  stream_failure: Problem | null;
  payload_bytes_by_cursor: ReadonlyMap<string, Uint8Array>;
  agent_outputs_by_cursor: ReadonlyMap<string, AgentOutputProjection>;
}

/** The served rail vocabulary every state-reading surface shares (#89). */
export type NodeState = RunV3["node_rail"][number]["state"];

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
    stream_failure: null,
    payload_bytes_by_cursor: new Map(),
    agent_outputs_by_cursor: new Map()
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
    protocol_problem: null,
    stream_failure: null
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

export function markFailed(
  projection: StreamProjection,
  problem: Problem | null
): StreamProjection {
  return { ...projection, connection: "failed", stream_failure: problem };
}

export async function decodeAndApplyDurableEvent(
  projection: StreamProjection,
  rawData: string
): Promise<StreamProjection> {
  if (projection.protocol_problem !== null) return projection;
  let frame: StreamFrame;
  try {
    frame = decodeStreamFrame(JSON.parse(rawData));
  } catch {
    return { ...projection, protocol_problem: { type: "decoder" } };
  }
  if (isStreamFailure(frame)) return markFailed(projection, frame.problem);
  if (isRunProjectionCorrupt(frame)) {
    return { ...projection, protocol_problem: { type: "decoder" } };
  }
  const decoded: RunEvent = frame;
  let output: AgentOutputProjection | null = null;
  if (decoded.event === "AGENT_COMPLETED") {
    const bytes = decodeCanonicalBase64(decoded.output_base64);
    if (bytes === null) {
      return { ...projection, protocol_problem: { type: "decoder" } };
    }
    const received = await sha256Hex(bytes);
    if (received !== decoded.output_hash) {
      return {
        ...projection,
        protocol_problem: {
          type: "output_integrity",
          cursor: decoded.cursor,
          expected: decoded.output_hash,
          received
        }
      };
    }
    output = classifyAgentOutput(bytes, decoded.output_base64);
  }
  const applied = applyDurableEvent(projection, rawData, decoded);
  if (output === null || applied === projection || applied.protocol_problem !== null) {
    return applied;
  }
  const outputs = new Map(applied.agent_outputs_by_cursor);
  outputs.set(decoded.cursor, output);
  return { ...applied, agent_outputs_by_cursor: outputs };
}

function classifyAgentOutput(
  bytes: Uint8Array,
  canonicalBase64: string
): AgentOutputProjection {
  if (bytes.byteLength === 0) return { kind: "empty", value: "", byte_count: 0 };
  try {
    return {
      kind: "utf8",
      value: new TextDecoder("utf-8", { fatal: true }).decode(bytes),
      byte_count: bytes.byteLength
    };
  } catch {
    return { kind: "binary", value: canonicalBase64, byte_count: bytes.byteLength };
  }
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
