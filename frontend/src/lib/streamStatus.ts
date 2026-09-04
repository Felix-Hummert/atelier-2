import type { ConnectionState, ProtocolProblem } from "./runProjection";

const connectionLabels = {
  connecting: "Connecting",
  live: "Live",
  reconnecting: "Reconnecting",
  complete: "Complete",
  failed: "Stopped"
} as const satisfies Record<ConnectionState, string>;

export interface StreamStatusView {
  connection: ConnectionState;
  protocol_problem: ProtocolProblem | null;
}

export function streamStopped(value: StreamStatusView): boolean {
  return value.protocol_problem !== null || value.connection === "failed";
}

export function connectionLabel(value: StreamStatusView): string {
  return value.protocol_problem === null ? connectionLabels[value.connection] : "Stopped";
}

export function protocolTitle(value: { protocol_problem: ProtocolProblem | null }): string | null {
  if (value.protocol_problem === null) return null;
  return {
    decoder: "Event invalid",
    sequence_gap: "Event gap",
    conflicting_duplicate: "Event conflict",
    output_integrity: "Output mismatch"
  }[value.protocol_problem.type];
}

export function protocolDetail(value: { protocol_problem: ProtocolProblem | null }): string | null {
  const problem = value.protocol_problem;
  if (problem === null) return null;
  if (problem.type === "sequence_gap") {
    return `Confirmed sequence ${problem.expected - 1}; received ${problem.received}.`;
  }
  if (problem.type === "conflicting_duplicate") {
    return `Durable cursor ${problem.cursor} was replayed with different bytes.`;
  }
  if (problem.type === "output_integrity") {
    return `Computed SHA-256 ${problem.received}; durable output says ${problem.expected}.`;
  }
  return "A durable event did not match the closed wire contract.";
}
