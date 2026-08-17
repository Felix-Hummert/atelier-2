import { describe, expect, it, vi } from "vitest";

import { createCockpitApi, type RunEventHandlers } from "../../src/api/client";
import {
  decodeAndApplyDurableEvent,
  streamProjection
} from "../../src/lib/runProjection";

describe("native durable event transport", () => {
  it("opens the exact same-origin event route and forwards known and unknown message frames", async () => {
    const source = new FakeEventSource();
    const factory = vi.fn(() => source);
    let projection = streamProjection("run1.cnVu", "a".repeat(64));
    let eventQueue = Promise.resolve();
    const handlers: RunEventHandlers = {
      opened: vi.fn(),
      event: vi.fn((rawData) => {
        eventQueue = eventQueue.then(async () => {
          projection = await decodeAndApplyDurableEvent(projection, rawData);
        });
      }),
      disconnected: vi.fn()
    };
    const api = createCockpitApi(fetch, factory);

    const subscription = api.openRunEvents("run1.cnVu", handlers);
    source.dispatch("open", new Event("open"));
    source.dispatch("message", message(agentCompleted()));
    source.dispatch("message", message({ ...agentCompleted(), event: "NODE_PROGRESS" }));
    source.dispatch("error", new Event("error"));
    await eventQueue;

    expect(factory).toHaveBeenCalledWith("/atelier/api/v1/runs/run1.cnVu/events");
    expect(handlers.opened).toHaveBeenCalledTimes(1);
    expect(handlers.event).toHaveBeenCalledTimes(2);
    expect(projection.events).toHaveLength(1);
    expect(projection.protocol_problem).toEqual({ type: "decoder" });
    expect(handlers.disconnected).toHaveBeenCalledTimes(1);
    subscription.close();
    expect(source.closed).toBe(true);
  });

  it("binds a fetched workflow projection to the exact requested revision", async () => {
    const requested = "a".repeat(64);
    const other = "b".repeat(64);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        revision_hash: other,
        document_base64: "",
        graph: {
          format_version: 1,
          start_node_id: "final",
          nodes: [
            {
              type: "subworkflow",
              node_id: "final",
              operation: "add",
              operands: [2, 3],
              next_node_id: null
            }
          ]
        }
      })
    );

    await expect(createCockpitApi(fetcher).getWorkflowRevision(requested)).rejects.toThrow(
      "did not match the requested revision"
    );
    expect(fetcher).toHaveBeenCalledWith(
      `/atelier/api/v1/workflow-revisions/${requested}`,
      expect.any(Object)
    );
  });
});

function message(data: unknown): MessageEvent<string> {
  return new MessageEvent("message", { data: JSON.stringify(data) });
}

async function sha256Of(text: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(text)
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function chainedAgentCompleted(
  nodeId: string,
  output: string,
  sequence: number
) {
  // The shape #249 put on the wire and #253 taught the CLI: a format-3
  // completion carries its output base64 beside its hash, and the rail rides
  // along. This is the event an operator's own chain run produces.
  return {
    workflow_format_version: 3,
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: "a".repeat(64),
    node_id: nodeId,
    node_execution_id: "b".repeat(64),
    event_hash: "c".repeat(64),
    node_rail: [{ node_id: nodeId, state: "working", attempt: null }],
    event: "AGENT_COMPLETED",
    output_base64: btoa(output),
    // Computed, not invented: the projection verifies that the hash really is
    // the digest of the bytes, so a made-up one would prove the guard and not
    // the decoding.
    output_hash: await sha256Of(output),
    attempt_id: "e".repeat(64),
    attempt_ordinal: 1
  };
}

const V3_ANSWER = '"approved, with the second paragraph rewritten"';

function chainedWaitBase(sequence: number) {
  return {
    workflow_format_version: 3,
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: "a".repeat(64),
    node_id: "approve",
    node_execution_id: "b".repeat(64),
    event_hash: "c".repeat(64),
    node_rail: [{ node_id: "approve", state: "needs_you", attempt: null }]
  };
}

function chainedWaitingInput(sequence: number) {
  // No `answer_type`: a format-3 wait declares a schema, and naming a type here
  // would be the wire claiming a fact the document never wrote.
  return { ...chainedWaitBase(sequence), event: "WAITING_INPUT" };
}

async function chainedWaitAnswered(answer: string, sequence: number) {
  return {
    ...chainedWaitBase(sequence),
    node_rail: [{ node_id: "approve", state: "succeeded", attempt: null }],
    event: "WAIT_ANSWERED",
    answer_base64: btoa(answer),
    answer_hash: await sha256Of(answer)
  };
}

function agentCompleted() {
  return {
    cursor: "event1.cnVu.1",
    sequence: 1,
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: "a".repeat(64),
    node_id: "agent",
    node_execution_id: "b".repeat(64),
    event_hash: "c".repeat(64),
    event: "AGENT_COMPLETED",
    output: "done",
    payload_hash: "d".repeat(64)
  };
}

describe("a chain run seen while it runs", () => {
  it("carries every format-3 completion into the projection with its output", async () => {
    let projection = streamProjection("run1.cnVu", "a".repeat(64));

    projection = await decodeAndApplyDurableEvent(
      projection,
      JSON.stringify(await chainedAgentCompleted("code", "the draft", 1))
    );
    projection = await decodeAndApplyDurableEvent(
      projection,
      JSON.stringify(await chainedAgentCompleted("review", "the findings", 2))
    );

    expect(projection.protocol_problem).toBeNull();
    expect(projection.events).toHaveLength(2);
    expect(projection.events.map((entry) => entry.node_id)).toEqual([
      "code",
      "review"
    ]);
    const outputs = [...projection.agent_outputs_by_cursor.values()];
    expect(outputs.map((entry) => entry.value)).toEqual([
      "the draft",
      "the findings"
    ]);
  });

  it("reads a format-3 pause and the answer that carries it on, answer bytes and all", async () => {
    let projection = streamProjection("run1.cnVu", "a".repeat(64));

    projection = await decodeAndApplyDurableEvent(
      projection,
      JSON.stringify(chainedWaitingInput(1))
    );
    projection = await decodeAndApplyDurableEvent(
      projection,
      JSON.stringify(await chainedWaitAnswered(V3_ANSWER, 2))
    );

    expect(projection.protocol_problem).toBeNull();
    expect(projection.events.map((entry) => entry.event)).toEqual([
      "WAITING_INPUT",
      "WAIT_ANSWERED"
    ]);
    const answered = projection.events.at(-1);
    // A format-3 answer is whatever the wait's own schema admits, so it reaches
    // the browser base64 rather than as the decimal text the older formats send.
    expect(answered).toMatchObject({ answer_base64: btoa(V3_ANSWER) });
    expect(answered).not.toHaveProperty("answer");
  });

  it.each([
    [
      "an answer in the decimal shape only an older format's node can promise",
      async () => ({ ...(await chainedWaitAnswered(V3_ANSWER, 1)), answer: "17" })
    ],
    [
      "a pause naming an answer type the format never declares",
      async () => ({ ...chainedWaitingInput(1), answer_type: "integer" })
    ]
  ])("refuses a format-3 wait event carrying %s", async (_name, build) => {
    const projection = await decodeAndApplyDurableEvent(
      streamProjection("run1.cnVu", "a".repeat(64)),
      JSON.stringify(await build())
    );

    expect(projection.protocol_problem).toEqual({ type: "decoder" });
    expect(projection.events).toHaveLength(0);
  });
});

class FakeEventSource {
  closed = false;
  private readonly listeners = new Map<string, EventListener[]>();

  addEventListener(type: string, listener: EventListener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  dispatch(type: string, event: Event): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" }
  });
}
