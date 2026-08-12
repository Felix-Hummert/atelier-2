import { describe, expect, it } from "vitest";

import {
  applyDurableEvent,
  confirmResource,
  decodeAndApplyDurableEvent,
  failResource,
  markConnecting,
  markLive,
  startLoading,
  streamProjection
} from "../../src/lib/runProjection";

const digest = "a".repeat(64);

describe("retained resource truth", () => {
  it("keeps confirmed data visible while a refresh loads and later fails", () => {
    const confirmed = confirmResource({ confirmed: null, request: { state: "idle" } }, { id: "run" });
    const loading = startLoading(confirmed);
    const failed = failResource(loading, problem());

    expect(loading.confirmed).toEqual({ id: "run" });
    expect(failed.confirmed).toEqual({ id: "run" });
    expect(failed.request.state).toBe("failed");
  });
});

describe("contiguous durable SSE projection", () => {
  it("accepts the next sequence and an exact byte-equal replay", () => {
    const raw = JSON.stringify(event(1));
    const applied = applyDurableEvent(projection(), raw, event(1));
    const replayed = applyDurableEvent(applied, raw, event(1));

    expect(replayed.events).toHaveLength(1);
    expect(replayed.last_sequence).toBe(1);
    expect(replayed.protocol_problem).toBeNull();
  });

  it("stops on a sequence gap without discarding confirmed events", () => {
    const first = applyDurableEvent(projection(), JSON.stringify(event(1)), event(1));
    const gap = applyDurableEvent(first, JSON.stringify(event(3)), event(3));

    expect(gap.protocol_problem).toEqual({ type: "sequence_gap", expected: 2, received: 3 });
    expect(gap.events).toEqual([event(1)]);
    expect(gap.last_sequence).toBe(1);
  });

  it("stops on a cursor replay whose canonical payload bytes differ", () => {
    const firstRaw = JSON.stringify(event(1));
    const first = applyDurableEvent(projection(), firstRaw, event(1));
    const conflict = applyDurableEvent(first, `${firstRaw} `, event(1));

    expect(conflict.protocol_problem).toEqual({ type: "conflicting_duplicate", cursor: event(1).cursor });
    expect(conflict.events).toEqual([event(1)]);
  });

  it("treats EventSource error as reconnecting without inventing an API problem", () => {
    const connected = markLive(markConnecting(projection()));
    const reconnecting = markConnecting(connected, true);
    expect(reconnecting.connection).toBe("reconnecting");
    expect(reconnecting.protocol_problem).toBeNull();
  });

  it("surfaces an inexact wire integer as a decoder protocol problem", () => {
    const unsafe = event(1) as Record<string, unknown>;
    unsafe.sequence = Number.MAX_SAFE_INTEGER + 1;
    const rejected = decodeAndApplyDurableEvent(projection(), JSON.stringify(unsafe));
    expect(rejected.protocol_problem).toEqual({ type: "decoder" });
    expect(rejected.events).toEqual([]);
  });

  it.each([
    ["another run", event(2, { public_run_reference: "run1.b3RoZXI", cursor: "event1.b3RoZXI.2" })],
    ["another workflow revision", event(2, { workflow_revision_hash: "b".repeat(64) })]
  ])("stops before applying a valid follow-up event from %s", (_case, followUp) => {
    const first = applyDurableEvent(projection(), JSON.stringify(event(1)), event(1));
    const rejected = applyDurableEvent(first, JSON.stringify(followUp), followUp);

    expect(rejected.protocol_problem).toEqual({ type: "decoder" });
    expect(rejected.events).toEqual([event(1)]);
    expect(rejected.last_sequence).toBe(1);
  });
});

function projection() {
  return streamProjection("run1.cnVu", digest);
}

function event(
  sequence: number,
  changes: Partial<{
    cursor: string;
    public_run_reference: string;
    workflow_revision_hash: string;
  }> = {}
) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: digest,
    node_id: "agent",
    node_execution_id: digest,
    event_hash: digest,
    event: "AGENT_COMPLETED" as const,
    output: "result",
    payload_hash: digest,
    ...changes
  };
}

function problem() {
  return {
    type: "urn:atelier2:problem:v1:temporarily-unavailable" as const,
    title: "Temporarily unavailable" as const,
    status: 503 as const,
    detail: "Retry later."
  };
}
