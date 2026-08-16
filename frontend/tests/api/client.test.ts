import { describe, expect, it, vi } from "vitest";

import {
  createCockpitApi,
  decodeProblem,
  decodeRun,
  decodeRunEvent,
  decodeWorkflowRevisionDetail,
  executableGraph,
  problemDefinitions,
  type Problem
} from "../../src/api/client";
import { workflowRevision } from "../support/workflowV1";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends <Value>() => Value extends Right ? 1 : 2
    ? true
    : false;
type Assert<Value extends true> = Value;
export type ProblemTypeIsClosed = Assert<
  Equal<Problem["type"], `urn:atelier2:problem:v1:${keyof typeof problemDefinitions}`>
>;
export type ProblemVariantIsExact = Assert<
  Equal<
    Extract<Problem, { type: "urn:atelier2:problem:v1:run-not-found" }>,
    {
      type: "urn:atelier2:problem:v1:run-not-found";
      title: "Run not found";
      status: 404;
      detail: string;
    }
  >
>;

const digest = "a".repeat(64);
const publicReference = "run1.cnVuLTE";

function event(event: string, fields: Record<string, unknown> = {}) {
  return {
    cursor: `event1.cnVuLTE.${fields.sequence ?? 1}`,
    sequence: fields.sequence ?? 1,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: "node",
    node_execution_id: digest,
    event_hash: digest,
    event,
    ...fields
  };
}

function v2Event(eventName: string, fields: Record<string, unknown> = {}) {
  return {
    ...event(eventName, fields),
    workflow_format_version: 2,
    node_rail: [{ node_id: "agent", state: "working", attempt: null }]
  };
}

const v2Attempt = { attempt_id: digest, attempt_ordinal: 1 };
const v2Cancellation = { ...v2Attempt, command_id: "cancel-1", replacement: "ONE" };

describe("closed API decoders", () => {
  it("decodes all four graph node variants and refuses unknown fields", () => {
    const decoded = decodeWorkflowRevisionDetail(workflowRevision());

    expect(executableGraph(decoded.graph).nodes.map((node) => node.type)).toEqual([
      "agent",
      "action",
      "wait",
      "subworkflow"
    ]);
    expect(() => decodeWorkflowRevisionDetail({ ...decoded, invented: true })).toThrow();
  });

  it.each([
    ["duplicate node", { start_node_id: "final", nodes: [terminal("final"), terminal("final")] }],
    ["missing start", { start_node_id: "missing", nodes: [terminal("final")] }],
    [
      "missing successor",
      {
        start_node_id: "agent",
        nodes: [
          { type: "agent", node_id: "agent", job: "work", output: "result", next_node_id: "missing" },
          terminal("final")
        ]
      }
    ],
    [
      "unreachable node",
      {
        start_node_id: "wait",
        nodes: [
          { type: "wait", node_id: "wait", answer_type: "integer", next_node_id: "final" },
          { type: "wait", node_id: "lost", answer_type: "integer", next_node_id: "final" },
          terminal("final")
        ]
      }
    ]
  ])("refuses a structurally invalid projected graph: %s", (_case, graph) => {
    expect(() =>
      decodeWorkflowRevisionDetail({
        revision_hash: digest,
        document_base64: "",
        graph: { format_version: 1, ...graph }
      })
    ).toThrow();
  });

  it.each(["not-base64", "YQ", "YQ===", "Y Q==", "YQ-_", "===="])(
    "refuses a noncanonical document base64 value: %s",
    (document_base64) => {
      expect(() =>
        decodeWorkflowRevisionDetail({
          revision_hash: digest,
          document_base64,
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
      ).toThrow();
    }
  );

  it("decodes a state-consistent waiting run and refuses an open waiting union", () => {
    const run = {
      run_id: "run-1",
      public_run_reference: publicReference,
      workflow_revision_hash: digest,
      state_version: 2,
      state: "WAITING_INPUT",
      current_node: {
        type: "wait",
        node_id: "wait",
        answer_type: "integer",
        next_node_id: "final"
      },
      waiting: { type: "WAITING_INPUT", node_id: "wait", answer_type: "integer" },
      terminal_hash: null,
      latest_event_cursor: "event1.cnVuLTE.2"
    };

    expect(decodeRun(run).state).toBe("WAITING_INPUT");
    expect(() => decodeRun({ ...run, waiting: { type: "SOMETHING_NEW" } })).toThrow();
  });

  it.each(["", "run2.YQ", "run1._w", "run1.YQ==", "run1.", "run1.@@", "run1.YQ="])(
    "refuses a malformed or noncanonical public reference: %s",
    (public_run_reference) => {
      expect(() => decodeRun(startedRun({ public_run_reference }))).toThrow();
    }
  );

  it("binds the public reference to exact UTF-8 run_id and the latest cursor", () => {
    expect(decodeRun(startedRun()).run_id).toBe("run-1");
    expect(() => decodeRun(startedRun({ run_id: "other" }))).toThrow();
    expect(() =>
      decodeRun(startedRun({ latest_event_cursor: "event1.b3RoZXI.1" }))
    ).toThrow();
    expect(
      decodeRun(
        startedRun({
          run_id: "Grüße-東京",
          public_run_reference: "run1.R3LDvMOfZS3mnbHkuqw",
          latest_event_cursor: "event1.R3LDvMOfZS3mnbHkuqw.1"
        })
      ).run_id
    ).toBe("Grüße-東京");
  });

  it.each([
    event("AGENT_COMPLETED", { output: "answer", payload_hash: digest }),
    event("ACTION_RECONCILIATION_REQUIRED", { request_base64: "eA==", request_hash: digest }),
    event("ACTION_RECONCILIATION_RESOLVED", { receipt: receipt() }),
    event("ACTION_COMPLETED", { receipt: receipt() }),
    event("WAITING_INPUT", { answer_type: "integer" }),
    event("WAIT_ANSWERED", { answer: "17", answer_hash: digest }),
    event("SUBWORKFLOW_COMPLETED", { result: 5, result_hash: digest })
  ])("decodes the closed durable event union: $event", (value) => {
    expect(decodeRunEvent(value).event).toBe(value.event);
  });

  it.each([
    v2Event("AGENT_COMPLETED", { ...v2Attempt, output_base64: "YW5zd2Vy", output_hash: digest }),
    v2Event("AGENT_FAILED", { ...v2Attempt, failure_code: "PROCESS_EXITED_UNSUCCESSFULLY" }),
    v2Event("AGENT_CANCEL_REQUESTED", v2Cancellation),
    v2Event("AGENT_CANCELLED", {
      ...v2Cancellation,
      disposition: "REAPED_AFTER_TERM",
      replacement_attempt_id: "b".repeat(64)
    }),
    v2Event("AGENT_INTERRUPTED", {
      ...v2Cancellation,
      replacement: "NONE",
      disposition: "OWNER_LOST_AFTER_PARENT_DEATH",
      replacement_attempt_id: null
    }),
    v2Event("ACTION_RECONCILIATION_REQUIRED", { request_base64: "eA==", request_hash: digest }),
    v2Event("ACTION_RECONCILIATION_RESOLVED", { receipt: receipt() }),
    v2Event("ACTION_COMPLETED", { receipt: receipt() }),
    v2Event("WAITING_INPUT", { answer_type: "integer" }),
    v2Event("WAIT_ANSWERED", { answer: "17", answer_hash: digest }),
    v2Event("SUBWORKFLOW_COMPLETED", { result: 5, result_hash: digest })
  ])("decodes every public V2 event member: $event", (value) => {
    expect(decodeRunEvent(value).event).toBe(value.event);
  });

  it("refuses an unknown V2 event member instead of dropping it", () => {
    expect(() => decodeRunEvent(v2Event("NODE_PROGRESS", { percent: 50 }))).toThrow();
  });

  it("refuses an unknown durable event kind", () => {
    expect(() => decodeRunEvent(event("NODE_PROGRESS", { percent: 50 }))).toThrow();
  });

  it.each(["01", "+1", "-0", " 1", "1 ", "1.0", ""])(
    "refuses a noncanonical WAIT_ANSWERED integer: %s",
    (answer) => {
      expect(() =>
        decodeRunEvent(event("WAIT_ANSWERED", { answer, answer_hash: digest }))
      ).toThrow();
    }
  );

  it.each(["YQ", "YQ===", "Y Q==", "YQ-_", "===="])(
    "refuses noncanonical standard base64 in nested request/results: %s",
    (encoded) => {
      expect(() =>
        decodeRunEvent(
          event("ACTION_RECONCILIATION_REQUIRED", {
            request_base64: encoded,
            request_hash: digest
          })
        )
      ).toThrow();
      expect(() =>
        decodeRunEvent(
          event("ACTION_COMPLETED", {
            receipt: { ...receipt(), result_base64: encoded }
          })
        )
      ).toThrow();
    }
  );

  it("accepts only exactly representable integers at every numeric boundary", () => {
    expect(
      decodeRunEvent(
        event("SUBWORKFLOW_COMPLETED", {
          result: Number.MAX_SAFE_INTEGER,
          result_hash: digest
        })
      ).event
    ).toBe("SUBWORKFLOW_COMPLETED");
    for (const invalid of [Number.MAX_SAFE_INTEGER + 1, true, 1.5]) {
      expect(() =>
        decodeRunEvent(
          event("SUBWORKFLOW_COMPLETED", { result: invalid, result_hash: digest })
        )
      ).toThrow();
    }
  });

  it("refuses a cursor whose run or sequence disagrees with the event", () => {
    expect(() =>
      decodeRunEvent({ ...event("WAITING_INPUT", { answer_type: "integer" }), cursor: "event1.b3RoZXI.1" })
    ).toThrow();
    expect(() =>
      decodeRunEvent({ ...event("WAITING_INPUT", { answer_type: "integer" }), cursor: "event1.cnVuLTE.2" })
    ).toThrow();
  });

  it.each([
    "event1.cnVuLTE.0",
    "event1.cnVuLTE.01",
    "event1.cnVuLTE.-1",
    "event1.cnVuLTE.+1",
    "event2.cnVuLTE.1",
    "event1.cnVuLTE==.1",
    "event1..1"
  ])("refuses a malformed or noncanonical event cursor: %s", (cursor) => {
    expect(() =>
      decodeRunEvent({ ...event("WAITING_INPUT", { answer_type: "integer" }), cursor })
    ).toThrow();
  });

  it("validates base64 inside waiting projections and pending found commands", () => {
    const waiting = {
      ...startedRun(),
      state: "WAITING_RECONCILIATION",
      current_node: { type: "action", node_id: "action", next_node_id: "final" },
      waiting: {
        type: "WAITING_RECONCILIATION",
        node_id: "action",
        logical_effect_key: "effect",
        request_hash: digest,
        request_base64: "not-base64",
        intent_state_version: 1,
        pending_command: null
      }
    };
    expect(() => decodeRun(waiting)).toThrow();
    expect(() =>
      decodeRun({
        ...waiting,
        waiting: {
          ...waiting.waiting,
          request_base64: "eA==",
          pending_command: {
            command_id: "command",
            actor: "operator",
            evidence: "inspected",
            state: "PENDING",
            determination: {
              type: "operator_found",
              effect_id: "effect",
              result_base64: "YQ"
            }
          }
        }
      })
    ).toThrow();
  });

  it("decodes only the documented RFC 9457 problem union", () => {
    const problem = decodeProblem({
      type: "urn:atelier2:problem:v1:run-not-found",
      title: "Run not found",
      status: 404,
      detail: "Use a durable run."
    });
    expect(problem.type).toBe("urn:atelier2:problem:v1:run-not-found");
    expect(() => decodeProblem({ ...problem, type: "urn:atelier2:problem:v1:new-problem" })).toThrow();
  });

  it.each(Object.entries(problemDefinitions))(
    "binds problem %s to its exact title and status",
    (code, definition) => {
      const exact = {
        type: `urn:atelier2:problem:v1:${code}`,
        title: definition.title,
        status: definition.status,
        detail: "operation-specific detail"
      };
      expect(decodeProblem(exact).detail).toBe("operation-specific detail");
      expect(() => decodeProblem({ ...exact, title: "Wrong" })).toThrow();
      expect(() => decodeProblem({ ...exact, status: definition.status + 1 })).toThrow();
    }
  );

  it("matches the complete R2 problem definition matrix", () => {
    expect(problemDefinitions).toEqual(expectedProblemDefinitions);
  });
});

const configurationInput = {
  model: "claude-opus-5",
  auth_profile_revision_hash: digest,
  executor_revision: "claude-subscription/v1"
};

function configurationRevision(echo: Record<string, unknown>) {
  return {
    ...configurationInput,
    provider_id: "anthropic",
    auth_mode: "subscription",
    agent_configuration_revision_hash: digest,
    ...echo
  };
}

function publishing(revision: unknown) {
  return vi.fn<typeof fetch>().mockResolvedValue(
    new Response(JSON.stringify(revision), {
      status: 201,
      headers: { "content-type": "application/json" }
    })
  );
}

function sentBody(fetcher: ReturnType<typeof publishing>): unknown {
  return JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body));
}

describe("agent configuration publication", () => {
  it("sends the requested capability and carries the echoed value back", async () => {
    const fetcher = publishing(configurationRevision({ requested_capability: "interactive" }));

    const published = await createCockpitApi(fetcher).publishAgentConfiguration({
      ...configurationInput,
      requested_capability: "interactive"
    });

    expect(sentBody(fetcher)).toEqual({
      ...configurationInput,
      requested_capability: "interactive"
    });
    expect(published.value.requested_capability).toBe("interactive");
  });

  it("leaves the capability out when none was requested and accepts the headless echo", async () => {
    const fetcher = publishing(configurationRevision({ requested_capability: "headless" }));

    const published = await createCockpitApi(fetcher).publishAgentConfiguration(configurationInput);

    expect(sentBody(fetcher)).toEqual(configurationInput);
    expect(published.value.requested_capability).toBe("headless");
  });

  it.each([
    ["omits the capability echo", configurationRevision({})],
    ["echoes a capability outside the contract", configurationRevision({ requested_capability: "supervised" })]
  ])("refuses a publication response that %s", async (_case, revision) => {
    const fetcher = publishing(revision);

    await expect(
      createCockpitApi(fetcher).publishAgentConfiguration(configurationInput)
    ).rejects.toThrow("did not match the durable wire contract");
  });
});

const expectedProblemDefinitions = {
  "auth-profile-revision-conflict": { status: 409, title: "Auth profile revision conflict" },
  "auth-profile-revision-collision": { status: 409, title: "Auth profile revision collision" },
  "auth-profile-revision-not-found": { status: 404, title: "Auth profile revision not found" },
  "agent-executor-binding-unavailable": { status: 409, title: "Agent executor binding unavailable" },
  "agent-configuration-revision-collision": { status: 409, title: "Agent configuration revision collision" },
  "agent-configuration-revision-not-found": { status: 404, title: "Agent configuration revision not found" },
  "invalid-agent-bindings": { status: 422, title: "Invalid agent bindings" },
  "invalid-public-run-reference": { status: 400, title: "Invalid public run reference" },
  "invalid-event-cursor": { status: 400, title: "Invalid event cursor" },
  "invalid-revision-hash": { status: 400, title: "Invalid revision hash" },
  "event-cursor-run-mismatch": { status: 409, title: "Event cursor belongs to another run" },
  "event-cursor-ahead": { status: 409, title: "Event cursor is ahead of durable history" },
  "invalid-request": { status: 422, title: "Invalid request" },
  "invalid-base64": { status: 422, title: "Invalid base64" },
  "invalid-workflow-document": { status: 422, title: "Invalid workflow document" },
  "unsupported-media-type": { status: 415, title: "Unsupported media type" },
  "not-acceptable": { status: 406, title: "Not acceptable" },
  "workflow-revision-not-found": { status: 404, title: "Workflow revision not found" },
  "run-not-found": { status: 404, title: "Run not found" },
  "node-not-found": { status: 404, title: "Node not found" },
  "revision-collision": { status: 409, title: "Workflow revision collision" },
  "run-identity-conflict": { status: 409, title: "Run identity conflict" },
  "answer-revision-conflict": { status: 409, title: "Answer revision conflict" },
  "answer-state-conflict": { status: 409, title: "Answer state conflict" },
  "answer-bytes-conflict": { status: 409, title: "Answer bytes conflict" },
  "reconciliation-target-missing": { status: 409, title: "Reconciliation target missing" },
  "reconciliation-stale": { status: 409, title: "Reconciliation is stale" },
  "reconciliation-command-conflict": { status: 409, title: "Reconciliation command conflict" },
  "reconciliation-determination-conflict": {
    status: 409,
    title: "Reconciliation determination conflict"
  },
  "reconciliation-rejected": { status: 409, title: "Reconciliation was rejected" },
  "route-not-found": { status: 404, title: "Route not found" },
  "method-not-allowed": { status: 405, title: "Method not allowed" },
  "temporarily-unavailable": { status: 503, title: "Temporarily unavailable" },
  "durable-state-corrupt": { status: 500, title: "Durable state is corrupt" },
  "internal-error": { status: 500, title: "Internal error" }
} as const;

function startedRun(changes: Record<string, unknown> = {}) {
  return {
    run_id: "run-1",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    state_version: 1,
    state: "STARTED",
    current_node: {
      type: "agent",
      node_id: "agent",
      job: "work",
      output: "output",
      next_node_id: "final"
    },
    waiting: { type: "NONE" },
    terminal_hash: null,
    latest_event_cursor: "event1.cnVuLTE.1",
    ...changes
  };
}

function receipt() {
  return {
    logical_effect_key: "effect-key",
    request_hash: digest,
    effect_id: "effect",
    result_hash: digest,
    result_base64: "",
    confirmation_source: "OPERATOR_FOUND",
    reconcile_command_id: "command"
  };
}

function terminal(node_id: string) {
  return {
    type: "subworkflow",
    node_id,
    operation: "add",
    operands: [2, 3],
    next_node_id: null
  };
}

/**
 * The listing the cockpit asks for is a different shape from the one the route
 * answers by default, so the selector that asks for it is production behaviour
 * and not a detail of the URL. This double answers like the real route: the
 * enriched shape only when the selector is there, the frozen hash-only shape
 * otherwise. Drop `view=described` and the strict decoder meets a row without a
 * name and throws -- which is what an operator would meet.
 */
function servingRevisionsByView() {
  return vi.fn<typeof fetch>().mockImplementation(async (target) => {
    const described = String(target).includes("view=described");
    const item = described
      ? {
          revision_hash: digest,
          format_version: 3,
          executable: false,
          name: "Nightly regression sweep",
          description: "Runs the sweep and files what it finds."
        }
      : { revision_hash: digest };
    return new Response(
      JSON.stringify({ items: [item], next_after_revision_hash: null }),
      { status: 200, headers: { "content-type": "application/json" } }
    );
  });
}

describe("the saved-workflow listing the cockpit asks for", () => {
  it("asks the route for the described view and decodes a non-empty page of it", async () => {
    const fetcher = servingRevisionsByView();

    const page = await createCockpitApi(fetcher).listWorkflowRevisions();

    expect(String(fetcher.mock.calls[0]?.[0])).toContain("view=described");
    expect(page.items).toEqual([
      {
        revision_hash: digest,
        format_version: 3,
        executable: false,
        name: "Nightly regression sweep",
        description: "Runs the sweep and files what it finds."
      }
    ]);
  });
});
