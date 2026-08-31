import { describe, expect, it } from "vitest";

import {
  applyDurableEvent,
  decodeAndApplyDurableEvent,
  markConnecting,
  markLive,
  readableResult,
  restartStreamProjection,
  streamProjection
} from "../../src/lib/runProjection";
import {
  eventCursor,
  publicReference,
  revisionHash as digest,
  waitingInput as event
} from "../support/runV3";

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

  it("restarts after a protocol problem without forgetting confirmed bytes or accepting a conflicting replay", () => {
    const raw = JSON.stringify(event(1));
    const confirmed = applyDurableEvent(projection(), raw, event(1));
    const failed = applyDurableEvent(confirmed, JSON.stringify(event(3)), event(3));

    const restarted = restartStreamProjection(failed, publicReference, digest);

    expect(restarted.events).toEqual([event(1)]);
    expect(restarted.last_sequence).toBe(1);
    expect(restarted.payload_bytes_by_cursor.get(event(1).cursor)).toEqual(
      new TextEncoder().encode(raw)
    );
    expect(restarted.connection).toBe("connecting");
    expect(restarted.protocol_problem).toBeNull();

    const replayed = applyDurableEvent(restarted, raw, event(1));
    expect(replayed.events).toHaveLength(1);
    expect(replayed.protocol_problem).toBeNull();

    const conflict = applyDurableEvent(replayed, `${raw} `, event(1));
    expect(conflict.events).toHaveLength(1);
    expect(conflict.protocol_problem).toEqual({
      type: "conflicting_duplicate",
      cursor: event(1).cursor
    });
  });

  it("treats EventSource error as reconnecting without inventing an API problem", () => {
    const connected = markLive(markConnecting(projection()));
    const reconnecting = markConnecting(connected, true);
    expect(reconnecting.connection).toBe("reconnecting");
    expect(reconnecting.protocol_problem).toBeNull();
  });

  it("surfaces an inexact wire integer as a decoder protocol problem", async () => {
    const unsafe = event(1) as Record<string, unknown>;
    unsafe.sequence = Number.MAX_SAFE_INTEGER + 1;
    const rejected = await decodeAndApplyDurableEvent(projection(), JSON.stringify(unsafe));
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

  it("names a RUN_PROJECTION_CORRUPT frame on a per-run stream as a decoder failure", async () => {
    const next = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify({
        event: "RUN_PROJECTION_CORRUPT",
        public_run_reference: publicReference,
        problem: {
          type: "urn:atelier2:problem:v1:durable-state-corrupt",
          title: "Durable state is corrupt",
          status: 500,
          detail: "Stop mutation and inspect the durable store."
        }
      })
    );
    expect(next.protocol_problem).toEqual({ type: "decoder" });
  });
});

describe("a stream that ends because it failed", () => {
  it("reports the server problem and separates a failed stream from a finished one", async () => {
    const confirmed = await decodeAndApplyDurableEvent(
      markLive(projection()),
      JSON.stringify(event(1))
    );

    const failed = await decodeAndApplyDurableEvent(
      confirmed,
      JSON.stringify(streamFailure())
    );

    expect(failed.connection).toBe("failed");
    expect(failed.stream_failure).toEqual(streamFailure().problem);
    expect(failed.protocol_problem).toBeNull();
    expect(failed.events).toEqual([event(1)]);
    expect(failed.last_sequence).toBe(1);
  });

  it("carries no problem before the stream fails", () => {
    expect(projection().connection).toBe("connecting");
    expect(projection().stream_failure).toBeNull();
  });

  it.each([
    ["an unknown problem type", { type: "urn:atelier2:problem:v1:invented", title: "Invented", status: 500, detail: "" }],
    ["a missing problem body", undefined]
  ])("refuses a failure frame that is not the closed contract: %s", async (_case, problemBody) => {
    const rejected = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify({ event: "STREAM_FAILED", problem: problemBody })
    );

    expect(rejected.protocol_problem).toEqual({ type: "decoder" });
    expect(rejected.connection).toBe("connecting");
    expect(rejected.stream_failure).toBeNull();
  });

  it("forgets the failure when the operator restarts the stream", async () => {
    const failed = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify(streamFailure())
    );

    const restarted = restartStreamProjection(failed, publicReference, digest);

    expect(restarted.connection).toBe("connecting");
    expect(restarted.stream_failure).toBeNull();
  });
});

describe("verified Agent output projection", () => {
  it.each([
    ["utf8", "R3LDvMOfZSDmnbHkuqw=", "d9f1fa3818c49d96dce2661015bdad90989df9e67244a7e5f1519ab466286332", "Grüße 東京", 14],
    ["binary", "/wA=", "ea5dbf9596d187e9500f23e9a680109475341cf4e81f7e043f7d97152c10772f", "/wA=", 2],
    ["empty", "", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "", 0]
  ] as const)("keeps exact %s bytes after SHA-256 verification", async (kind, outputBase64, outputHash, value, byteCount) => {
    const completed = agentCompletedEvent({ output_base64: outputBase64, output_hash: outputHash });

    const applied = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify(completed)
    );

    expect(applied.protocol_problem).toBeNull();
    expect(applied.events).toEqual([completed]);
    expect(applied.agent_outputs_by_cursor.get(completed.cursor)).toEqual({
      kind,
      value,
      byte_count: byteCount
    });
  });

  it("refuses contradictory output bytes before accepting either event or output", async () => {
    const completed = agentCompletedEvent({
      output_base64: "R3LDvMOfZSDmnbHkuqw=",
      output_hash: digest
    });

    const rejected = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify(completed)
    );

    expect(rejected.protocol_problem).toEqual({
      type: "output_integrity",
      cursor: completed.cursor,
      expected: digest,
      received: "d9f1fa3818c49d96dce2661015bdad90989df9e67244a7e5f1519ab466286332"
    });
    expect(rejected.events).toEqual([]);
    expect(rejected.agent_outputs_by_cursor.size).toBe(0);
  });
});

function projection() {
  return streamProjection(publicReference, digest);
}

function streamFailure() {
  return {
    event: "STREAM_FAILED" as const,
    problem: {
      type: "urn:atelier2:problem:v1:durable-state-corrupt" as const,
      title: "Durable state is corrupt" as const,
      status: 500 as const,
      detail: "Stop mutation and inspect the durable store."
    }
  };
}

function agentCompletedEvent(changes: Record<string, unknown> = {}) {
  return {
    workflow_format_version: 3 as const,
    cursor: eventCursor(1),
    sequence: 1,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: "agent",
    node_execution_id: digest,
    event_hash: digest,
    attempt_id: digest,
    attempt_ordinal: 1 as const,
    node_rail: [
      { node_id: "agent", state: "succeeded" as const, attempt: { ordinal: 1 as const, state: null } }
    ],
    event: "AGENT_COMPLETED" as const,
    output_base64: "",
    output_hash: digest,
    ...changes
  };
}

describe("a node's declared answer read as prose (#716)", () => {
  it("reads a bare string as itself, with nothing behind an Exact-text disclosure", () => {
    expect(readableResult("Three German sentences about code review.")).toEqual({
      kind: "text",
      text: "Three German sentences about code review.",
      raw: null
    });
  });

  it("reads a declared object's own answer field as the one sentence, an empty started_run_ids left out", () => {
    const raw = '{"answer":"The workflow could not be started.","started_run_ids":[]}';
    expect(readableResult(raw)).toEqual({
      kind: "object",
      sentence: "The workflow could not be started.",
      fields: [],
      raw
    });
  });

  it("shows a remaining non-empty field after the answer sentence -- nothing material only in the disclosure", () => {
    const raw = '{"answer":"Started the fix.","started_run_ids":["run1.a","run1.b"]}';
    expect(readableResult(raw)).toEqual({
      kind: "object",
      sentence: "Started the fix.",
      fields: [{ label: "started_run_ids", value: "run1.a, run1.b" }],
      raw
    });
  });

  it("reads an object with no answer field as all of its own fields, label by value", () => {
    const raw = '{"verdict":"green","findings":2}';
    expect(readableResult(raw)).toEqual({
      kind: "object",
      sentence: null,
      fields: [
        { label: "verdict", value: "green" },
        { label: "findings", value: "2" }
      ],
      raw
    });
  });

  it("reads a declared array as its own items, never as a JSON line", () => {
    const raw = '["one finding","another finding"]';
    expect(readableResult(raw)).toEqual({
      kind: "items",
      items: ["one finding", "another finding"],
      raw
    });
  });

  it("reads an array of declared objects by rendering each item's own value form", () => {
    const raw = '[{"id":"a"},{"id":"b"}]';
    expect(readableResult(raw)).toEqual({
      kind: "items",
      items: ['{"id":"a"}', '{"id":"b"}'],
      raw
    });
  });

  it("falls back to the raw text for a value no declared shape admits", () => {
    expect(readableResult("[]")).toEqual({ kind: "text", text: "[]", raw: null });
    expect(readableResult("{}")).toEqual({ kind: "text", text: "{}", raw: null });
  });
});
