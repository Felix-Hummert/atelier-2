import {
  decodeStreamFrame,
  isStreamFailure,
  type Problem,
  type RunEvent
} from "../api/client";
import type { ConnectionState, ProtocolProblem } from "./runProjection";

/**
 * The workbench's hold of `GET /events`. Connection states are the same words
 * as a per-run stream; sequence and identity stay on the server's resume.
 */
export type AttentionConnection = Exclude<ConnectionState, "complete">;

export interface AttentionHold {
  connection: AttentionConnection;
  protocol_problem: ProtocolProblem | null;
  stream_failure: Problem | null;
}

export function startAttentionHold(): AttentionHold {
  return { connection: "connecting", protocol_problem: null, stream_failure: null };
}

export function markAttentionLive(hold: AttentionHold): AttentionHold {
  if (attentionStopped(hold)) return hold;
  return { ...hold, connection: "live" };
}

export function markAttentionConnecting(
  hold: AttentionHold,
  reconnecting = false
): AttentionHold {
  if (attentionStopped(hold)) return hold;
  return { ...hold, connection: reconnecting ? "reconnecting" : "connecting" };
}

export function markAttentionFailed(
  hold: AttentionHold,
  problem: Problem | null
): AttentionHold {
  return { ...hold, connection: "failed", stream_failure: problem };
}

export function attentionStopped(hold: AttentionHold): boolean {
  return hold.protocol_problem !== null || hold.connection === "failed";
}

/**
 * The kinds this feed carries, as the door itself declares them
 * (`ATTENTION_EVENT_KINDS` in `adapters/dbos/attention_events.py`). A shorter
 * list here is not caution: a kind the door sends and this list omits is read
 * as a broken contract, which stops the hold on the first reconciliation the
 * workshop raises.
 */
export function isAttentionEvent(event: RunEvent): boolean {
  return (
    event.event === "WAITING_INPUT" ||
    event.event === "AGENT_FAILED" ||
    event.event === "ACTION_RECONCILIATION_REQUIRED"
  );
}

export function applyAttentionFrame(
  hold: AttentionHold,
  rawData: string
): { hold: AttentionHold; event: RunEvent | null } {
  if (attentionStopped(hold)) return { hold, event: null };
  let frame;
  try {
    frame = decodeStreamFrame(JSON.parse(rawData));
  } catch {
    return { hold: { ...hold, protocol_problem: { type: "decoder" } }, event: null };
  }
  if (isStreamFailure(frame)) {
    return { hold: markAttentionFailed(hold, frame.problem), event: null };
  }
  if (!isAttentionEvent(frame)) {
    return { hold: { ...hold, protocol_problem: { type: "decoder" } }, event: null };
  }
  return { hold, event: frame };
}
