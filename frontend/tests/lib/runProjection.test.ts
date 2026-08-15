import { describe, expect, it } from "vitest";

import {
  applyDurableEvent,
  confirmResource,
  decodeAndApplyDurableEvent,
  failResource,
  markConnecting,
  markLive,
  projectNodeRail,
  restartStreamProjection,
  startLoading,
  streamProjection
} from "../../src/lib/runProjection";
import type { RunV1, RunV2, WorkflowRevisionDetail } from "../../src/api/client";

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

  it("restarts after a protocol problem without forgetting confirmed bytes or accepting a conflicting replay", () => {
    const raw = JSON.stringify(event(1));
    const confirmed = applyDurableEvent(projection(), raw, event(1));
    const failed = applyDurableEvent(confirmed, JSON.stringify(event(3)), event(3));

    const restarted = restartStreamProjection(failed, "run1.cnVu", digest);

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

  it("refuses an event whose node identity or kind is absent from the bound graph", async () => {
    const wrongNode = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify(event(1, { node_id: "missing" })),
      workflow().graph
    );
    const wrongKind = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify({ ...waitInputEvent(1), node_id: "agent" }),
      workflow().graph
    );

    expect(wrongNode.protocol_problem).toEqual({ type: "decoder" });
    expect(wrongKind.protocol_problem).toEqual({ type: "decoder" });
  });

  it.each([
    ["V1 event with V2 graph", event(1), v2Scenario().graph],
    ["V2 event with V1 graph", v2AgentEvent("AGENT_COMPLETED"), workflow().graph]
  ] as const)("refuses a workflow/event format mismatch: %s", async (_case, mismatchedEvent, graph) => {
    const rejected = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify(mismatchedEvent),
      graph
    );

    expect(rejected.protocol_problem).toEqual({ type: "decoder" });
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

    const restarted = restartStreamProjection(failed, "run1.cnVu", digest);

    expect(restarted.connection).toBe("connecting");
    expect(restarted.stream_failure).toBeNull();
  });
});

describe("verified V2 Agent output projection", () => {
  it.each([
    ["utf8", "R3LDvMOfZSDmnbHkuqw=", "d9f1fa3818c49d96dce2661015bdad90989df9e67244a7e5f1519ab466286332", "Grüße 東京", 14],
    ["binary", "/wA=", "ea5dbf9596d187e9500f23e9a680109475341cf4e81f7e043f7d97152c10772f", "/wA=", 2],
    ["empty", "", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "", 0]
  ] as const)("keeps exact %s bytes after SHA-256 verification", async (kind, outputBase64, outputHash, value, byteCount) => {
    const completed = v2AgentEvent("AGENT_COMPLETED", { output_base64: outputBase64, output_hash: outputHash });

    const applied = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify(completed),
      v2Scenario().graph
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
    const completed = v2AgentEvent("AGENT_COMPLETED", {
      output_base64: "R3LDvMOfZSDmnbHkuqw=",
      output_hash: digest
    });

    const rejected = await decodeAndApplyDurableEvent(
      projection(),
      JSON.stringify(completed),
      v2Scenario().graph
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

describe("V2 Agent terminal truth", () => {
  it.each([
    ["AGENT_COMPLETED", "completed", "working"],
    ["AGENT_FAILED", "failed", "queued"],
    ["AGENT_CANCELLED", "cancelled", "queued"],
    ["AGENT_INTERRUPTED", "interrupted", "queued"]
  ] as const)("projects %s without inventing successor progress", (eventKind, state, successor) => {
    const scenario = v2Scenario();
    const rail = projectNodeRail(
      scenario.run,
      scenario.graph,
      [v2AgentEvent(eventKind)]
    );

    expect(rail.map((node) => node.state)).toEqual([state, successor]);
  });

  it.each(["AGENT_CANCELLED", "AGENT_INTERRUPTED"] as const)(
    "keeps a prepared replacement live after %s remains terminal history",
    (eventKind) => {
      const scenario = v2ReplacementScenario(eventKind);

      const rail = projectNodeRail(scenario.run, scenario.graph, [scenario.event]);

      expect(rail.map((node) => node.state)).toEqual(["working", "queued"]);
    }
  );
});

describe("read-only node rail", () => {
  it("orders the graph from its start edge and projects all four durable node states", () => {
    const events = [
      event(1),
      actionEvent(2, "ACTION_COMPLETED"),
      waitInputEvent(3)
    ];

    const rail = projectNodeRail(waitingRun(), workflow().graph, events);

    expect(rail.map(({ node }) => node.node_id)).toEqual(["agent", "action", "wait", "final"]);
    expect(rail.map(({ state }) => state)).toEqual(["done", "done", "needs_you", "queued"]);
    expect(rail.map(({ last_event }) => last_event?.event ?? null)).toEqual([
      "AGENT_COMPLETED",
      "ACTION_COMPLETED",
      "WAITING_INPUT",
      null
    ]);
  });

  it("shows an accepted reconciliation as Working even while the snapshot still waits", () => {
    const value = waitingRun();
    const pending: RunV1 = {
      ...value,
      state: "WAITING_RECONCILIATION",
      current_node: workflow().graph.nodes[1]! as RunV1["current_node"],
      waiting: {
        type: "WAITING_RECONCILIATION",
        node_id: "action",
        logical_effect_key: "effect",
        request_hash: digest,
        request_base64: "cmVxdWVzdA==",
        intent_state_version: 2,
        pending_command: {
          command_id: "command",
          actor: "operator",
          evidence: "inspected exact request",
          state: "PENDING",
          determination: { type: "operator_authoritative_absence" }
        }
      }
    };

    expect(projectNodeRail(pending, workflow().graph, [event(1)])[1]?.state).toBe("working");
  });

  it("keeps the authoritative snapshot while replay catches up, then advances from later events", () => {
    const initial = startedRun();
    const caughtUp = projectNodeRail(initial, workflow().graph, [event(1)]);
    const newlyWaiting = projectNodeRail(initial, workflow().graph, [
      event(1),
      reconciliationRequiredEvent(2)
    ]);
    const advanced = projectNodeRail(initial, workflow().graph, [
      event(1),
      reconciliationRequiredEvent(2),
      actionEvent(3, "ACTION_COMPLETED")
    ]);

    expect(caughtUp.map(({ state }) => state)).toEqual(["done", "working", "queued", "queued"]);
    expect(newlyWaiting.map(({ state }) => state)).toEqual(["done", "needs_you", "queued", "queued"]);
    expect(advanced.map(({ state }) => state)).toEqual(["done", "done", "working", "queued"]);
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
    node_id: string;
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

function workflow(): WorkflowRevisionDetail {
  return {
    revision_hash: digest,
    document_base64: "",
    graph: {
      format_version: 1,
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

function waitingRun(): RunV1 {
  return {
    run_id: "run",
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: digest,
    state_version: 3,
    state: "WAITING_INPUT",
    current_node: workflow().graph.nodes[2]! as RunV1["current_node"],
    waiting: { type: "WAITING_INPUT", node_id: "wait", answer_type: "integer" },
    terminal_hash: null,
    latest_event_cursor: "event1.cnVu.3"
  };
}

function startedRun(): RunV1 {
  return {
    ...waitingRun(),
    state_version: 1,
    state: "STARTED",
    current_node: workflow().graph.nodes[1]! as RunV1["current_node"],
    waiting: { type: "NONE" },
    latest_event_cursor: "event1.cnVu.1"
  };
}

function actionEvent(sequence: number, kind: "ACTION_COMPLETED") {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: digest,
    node_id: "action",
    node_execution_id: digest,
    event_hash: digest,
    event: kind,
    receipt: {
      logical_effect_key: "effect",
      request_hash: digest,
      effect_id: "effect-1",
      result_hash: digest,
      result_base64: "cmVzdWx0",
      confirmation_source: "ADAPTER_EXECUTION" as const,
      reconcile_command_id: null
    }
  };
}

function waitInputEvent(sequence: number) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: digest,
    node_id: "wait",
    node_execution_id: digest,
    event_hash: digest,
    event: "WAITING_INPUT" as const,
    answer_type: "integer" as const
  };
}

function reconciliationRequiredEvent(sequence: number) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: digest,
    node_id: "action",
    node_execution_id: digest,
    event_hash: digest,
    event: "ACTION_RECONCILIATION_REQUIRED" as const,
    request_base64: "cmVxdWVzdA==",
    request_hash: digest
  };
}

function v2Scenario() {
  const graph = {
    format_version: 2 as const,
    start_node_id: "agent",
    nodes: [
      { type: "agent" as const, node_id: "agent", role: "builder", job: "Build", next_node_id: "final" },
      { type: "subworkflow" as const, node_id: "final", operation: "add" as const, operands: [2, 3] as [number, number], next_node_id: null }
    ]
  };
  const run: RunV2 = {
    workflow_format_version: 2,
    run_id: "run",
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: digest,
    agent_binding_set_hash: digest,
    agent_bindings: [],
    state_version: 1,
    state: "STARTED",
    current_node: graph.nodes[0]! as RunV2["current_node"],
    agent_attempts: [{
      attempt_id: digest, node_execution_id: digest, request_hash: digest,
      attempt_ordinal: 1, state: "POSSIBLY_RAN", failure_code: null, cancellation: null
    }],
    waiting: { type: "NONE" },
    terminal_hash: null,
    latest_event_cursor: null
  };
  return { graph, run };
}

function v2AgentEvent(
  eventKind: "AGENT_COMPLETED" | "AGENT_FAILED" | "AGENT_CANCELLED" | "AGENT_INTERRUPTED",
  changes: Record<string, unknown> = {}
) {
  const common = {
    workflow_format_version: 2 as const,
    cursor: "event1.cnVu.1",
    sequence: 1,
    public_run_reference: "run1.cnVu",
    workflow_revision_hash: digest,
    node_id: "agent",
    node_execution_id: digest,
    event_hash: digest,
    attempt_id: digest,
    attempt_ordinal: 1 as const
  };
  if (eventKind === "AGENT_COMPLETED") {
    return { ...common, event: eventKind, output_base64: "", output_hash: digest, ...changes };
  }
  if (eventKind === "AGENT_FAILED") {
    return { ...common, event: eventKind, failure_code: "PROCESS_EXITED_UNSUCCESSFULLY" as const };
  }
  return {
    ...common,
    event: eventKind,
    command_id: "cancel",
    replacement: "NONE" as const,
    disposition: "REAPED_AFTER_TERM" as const,
    replacement_attempt_id: null
  };
}

function v2ReplacementScenario(
  eventKind: "AGENT_CANCELLED" | "AGENT_INTERRUPTED"
) {
  const scenario = v2Scenario();
  const firstAttempt = scenario.run.agent_attempts[0]!;
  const replacementAttemptId = "b".repeat(64);
  return {
    ...scenario,
    run: {
      ...scenario.run,
      latest_event_cursor: "event1.cnVu.1",
      agent_attempts: [
        {
          ...firstAttempt,
          state: eventKind === "AGENT_CANCELLED" ? "CANCELLED" as const : "INTERRUPTED" as const,
          cancellation: {
            command_id: "cancel",
            replacement: "ONE" as const,
            redrive_state: "CLEANUP_ATTESTED" as const,
            disposition: "REAPED_AFTER_TERM" as const
          }
        },
        {
          ...firstAttempt,
          attempt_id: replacementAttemptId,
          attempt_ordinal: 2 as const,
          state: "PREPARED" as const
        }
      ]
    },
    event: {
      ...v2AgentEvent(eventKind),
      replacement: "ONE" as const,
      replacement_attempt_id: replacementAttemptId
    }
  };
}
