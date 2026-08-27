import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";

import {
  createCockpitApi,
  decodeProblem,
  decodeRun,
  decodeRunEvent,
  decodeStreamFrame,
  decodeWorkflowRevisionDetail,
  executableGraph,
  isRunV3,
  projectSourceConnectionRevisionSchema,
  problemDefinitions,
  type Problem,
  type RunProjectionCorrupt
} from "../../src/api/client";
import { cancelMutation } from "../../src/lib/mutationJournal";
import { cancellableBlock, notCancellableBlock } from "../support/runV3";
import { workflowRevision } from "../support/workflowV1";

const PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:";

/**
 * The frozen OpenAPI document is the one object both sides can read. The
 * schema-* problems are generated from SchemaDocumentRefusal, so a new enum
 * member publishes a type without anyone editing this file. Collecting every
 * type.const with the problem prefix, and the title.const and status.const
 * that sit beside it, is what makes that drift fail here.
 */
const servedDocument = JSON.parse(
  readFileSync(resolve(process.cwd(), "..", "tests", "api", "openapi_frozen.json"), "utf8")
) as {
  components: {
    schemas: Record<
      string,
      {
        properties?: {
          type?: { const?: string };
          title?: { const?: string };
          status?: { const?: number };
        };
      }
    >;
  };
};

function publishedProblemDefinitions(document: typeof servedDocument) {
  return Object.fromEntries(
    Object.values(document.components.schemas).flatMap((schema) => {
      const type = schema.properties?.type?.const;
      return typeof type === "string" && type.startsWith(PROBLEM_TYPE_PREFIX)
        ? [
            [
              type.slice(PROBLEM_TYPE_PREFIX.length),
              { status: schema.properties?.status?.const, title: schema.properties?.title?.const }
            ]
          ]
        : [];
    })
  );
}

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
export type RunProjectionCorruptProblemIsDurableStateCorrupt = Assert<
  Equal<
    RunProjectionCorrupt["problem"]["type"],
    "urn:atelier2:problem:v1:durable-state-corrupt"
  >
>;

const digest = "a".repeat(64);
const publicReference = "run1.cnVuLTE";

/** A published V3 revision whose two-node body is declared as a bounded, verdict-exited loop. */
function v3RevisionWithLoop() {
  return {
    workflow_revision_hash: digest,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3 as const,
      executable: true as const,
      not_executable_reason: null,
      node_count: 2,
      agent_roles: ["builder"],
      orders: [],
      wait_answer_schemas: [],
      node_previews: [
        {
          id: "implement",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Do the one thing this chain is for.",
          depends_on: []
        },
        {
          id: "review",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Check what the node before you did.",
          depends_on: ["implement"]
        }
      ],
      loops: [
        {
          id: "until_reviewed",
          member_node_ids: ["implement", "review"],
          maximum_rounds: 3,
          repeat_while: { node: "review", verdict: "revise" as const }
        }
      ],
      name: "Build and review until the review says it is done",
      description: null
    }
  };
}

/** The same two-node V3 revision, with no loop declared over its body. */
function v3RevisionWithoutLoop() {
  const withLoop = v3RevisionWithLoop();
  return {
    ...withLoop,
    graph: {
      ...withLoop.graph,
      loops: [],
      name: "Implement, then review, with no declared loop"
    }
  };
}

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

function v3Event(eventName: string, fields: Record<string, unknown> = {}) {
  return {
    ...event(eventName, fields),
    workflow_format_version: 3,
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

  it("decodes a declared loop's body, bound, and verdict exit", () => {
    const decoded = decodeWorkflowRevisionDetail(v3RevisionWithLoop());

    if (decoded.graph.workflow_format_version !== 3) throw new Error("the V3 fixture changed");
    expect(decoded.graph.loops).toEqual([
      {
        id: "until_reviewed",
        member_node_ids: ["implement", "review"],
        maximum_rounds: 3,
        repeat_while: { node: "review", verdict: "revise" }
      }
    ]);
  });

  it("decodes a graph that declares no loop as an empty loop list", () => {
    const decoded = decodeWorkflowRevisionDetail(v3RevisionWithoutLoop());

    if (decoded.graph.workflow_format_version !== 3) throw new Error("the V3 fixture changed");
    expect(decoded.graph.loops).toEqual([]);
  });

  it("refuses a loop verdict outside the closed vocabulary", () => {
    const revision = v3RevisionWithLoop();
    if (revision.graph.workflow_format_version !== 3) throw new Error("the V3 fixture changed");
    const [loop] = revision.graph.loops;
    if (loop === undefined) throw new Error("the loop fixture changed");

    expect(() =>
      decodeWorkflowRevisionDetail({
        ...revision,
        graph: {
          ...revision.graph,
          loops: [{ ...loop, repeat_while: { node: "review", verdict: "maybe" } }]
        }
      })
    ).toThrow();
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
        workflow_revision_hash: digest,
        document_base64: "",
        graph: { workflow_format_version: 1, ...graph }
      })
    ).toThrow();
  });

  it.each(["not-base64", "YQ", "YQ===", "Y Q==", "YQ-_", "===="])(
    "refuses a noncanonical document base64 value: %s",
    (document_base64) => {
      expect(() =>
        decodeWorkflowRevisionDetail({
          workflow_revision_hash: digest,
          document_base64,
          graph: {
            workflow_format_version: 1,
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

  it.each(["PROCESS_OUTPUT_LIMIT_EXCEEDED", "PROCESS_SUPERVISION_FAILED"])(
    "decodes a failed V2 run snapshot carrying the Runner failure code: %s",
    (failureCode) => {
      const decoded = decodeRun(failedV2Run(failureCode));

      expect(decoded.state).toBe("FAILED");
      expect("agent_attempts" in decoded && decoded.agent_attempts[0]?.failure_code).toBe(
        failureCode
      );
    }
  );

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
    v3Event("ACTION_RECONCILIATION_REQUIRED", { request_base64: "eA==", request_hash: digest }),
    v3Event("ACTION_RECONCILIATION_RESOLVED", { receipt: receipt() }),
    v3Event("ACTION_COMPLETED", { receipt: receipt() })
  ])("decodes the V3 Action event family: $event", (value) => {
    expect(decodeRunEvent(value).event).toBe(value.event);
  });

  it.each([
    v2Event("AGENT_COMPLETED", { ...v2Attempt, output_base64: "YW5zd2Vy", output_hash: digest }),
    v2Event("AGENT_FAILED", { ...v2Attempt, failure_code: "PROCESS_EXITED_UNSUCCESSFULLY" }),
    v2Event("AGENT_FAILED", { ...v2Attempt, failure_code: "PROCESS_OUTPUT_LIMIT_EXCEEDED" }),
    v2Event("AGENT_FAILED", { ...v2Attempt, failure_code: "PROCESS_SUPERVISION_FAILED" }),
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

  it.each(["PROCESS_OUTPUT_LIMIT_EXCEEDED", "PROCESS_SUPERVISION_FAILED"])(
    "decodes the new runner failure in both public event families: %s",
    (failureCode) => {
      expect(
        decodeRunEvent(v2Event("AGENT_FAILED", { ...v2Attempt, failure_code: failureCode })).event
      ).toBe("AGENT_FAILED");
      expect(
        decodeRunEvent(
          v3Event("AGENT_FAILED", {
            ...v2Attempt,
            failure_code: failureCode,
            reason: null
          })
        ).event
      ).toBe("AGENT_FAILED");
    }
  );

  it("refuses a failure code outside the published vocabulary", () => {
    expect(() =>
      decodeRunEvent(v2Event("AGENT_FAILED", { ...v2Attempt, failure_code: "RUNNER_BROKE" }))
    ).toThrow();
  });

  it("decodes the attempt-less executor refusal and refuses a forged attempt", () => {
    const refusal = { reason: "agent-executor-binding-unavailable" as const };

    expect(decodeRunEvent(v2Event("AGENT_FAILED", refusal))).toMatchObject(refusal);
    expect(decodeRunEvent(v3Event("AGENT_FAILED", refusal))).toMatchObject(refusal);
    expect(() => decodeRunEvent(v2Event("AGENT_FAILED", { ...refusal, ...v2Attempt }))).toThrow();
    expect(() => decodeRunEvent(v3Event("AGENT_FAILED", { ...refusal, ...v2Attempt }))).toThrow();
  });

  it("refuses an unknown durable event kind", () => {
    expect(() => decodeRunEvent(event("NODE_PROGRESS", { percent: 50 }))).toThrow();
  });

  it("decodes the attention feed's per-run corruption frame", () => {
    const frame = decodeStreamFrame({
      event: "RUN_PROJECTION_CORRUPT",
      public_run_reference: "run1.cnVu",
      problem: {
        type: "urn:atelier2:problem:v1:durable-state-corrupt",
        title: "Durable state is corrupt",
        status: 500,
        detail: "Stop mutation and inspect the durable store."
      }
    });
    expect(frame.event).toBe("RUN_PROJECTION_CORRUPT");
  });

  it("refuses a RUN_PROJECTION_CORRUPT frame with a foreign problem type", () => {
    expect(() =>
      decodeStreamFrame({
        event: "RUN_PROJECTION_CORRUPT",
        public_run_reference: "run1.cnVu",
        problem: {
          type: "urn:atelier2:problem:v1:internal-error",
          title: "Internal error",
          status: 500,
          detail: "Retry only after the server fault has been inspected."
        }
      })
    ).toThrow();
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

  it("decodes the library-document-ambiguous problem the recognition door answers", () => {
    const problem = decodeProblem({
      type: "urn:atelier2:problem:v1:library-document-ambiguous",
      title: "Document matches more than one library kind",
      status: 422,
      detail: "The document matches agent_definition and skill."
    });
    expect(problem.type).toBe("urn:atelier2:problem:v1:library-document-ambiguous");
    expect(problem.status).toBe(422);
  });

  it("decodes an agent-definition-revision-not-found problem the read door answers", () => {
    const problem = decodeProblem({
      type: "urn:atelier2:problem:v1:agent-definition-revision-not-found",
      title: "Agent definition revision not found",
      status: 404,
      detail: "Publish the exact agent definition revision before reading its fields."
    });
    expect(problem).toEqual({
      type: "urn:atelier2:problem:v1:agent-definition-revision-not-found",
      title: "Agent definition revision not found",
      status: 404,
      detail: "Publish the exact agent definition revision before reading its fields."
    });
  });

  it("decodes a published run-input-refused problem instead of calling it undocumented", () => {
    const problem = decodeProblem({
      type: "urn:atelier2:problem:v1:run-input-refused",
      title: "Run input refused",
      status: 422,
      detail: "Supply exactly the orders this workflow declares, each satisfying the schema its author pinned."
    });
    expect(problem).toEqual({
      type: "urn:atelier2:problem:v1:run-input-refused",
      title: "Run input refused",
      status: 422,
      detail: "Supply exactly the orders this workflow declares, each satisfying the schema its author pinned."
    });
  });

  it.each(Object.entries(problemDefinitions))(
    "binds problem %s to its exact title and status",
    (code, definition) => {
      const exact = {
        type: `urn:atelier2:problem:v1:${code}`,
        title: definition.title,
        status: definition.status,
        detail: "operation-specific detail",
        ...(code === "uncast-agent-roles"
          ? { uncast_roles: [{ role: "reviewer", reason: "no-project-default" }] }
          : {})
      };
      expect(decodeProblem(exact).detail).toBe("operation-specific detail");
      expect(() => decodeProblem({ ...exact, title: "Wrong" })).toThrow();
      expect(() => decodeProblem({ ...exact, status: definition.status + 1 })).toThrow();
    }
  );

  it("decodes exactly the problem definitions the document publishes", () => {
    expect(problemDefinitions).toEqual(publishedProblemDefinitions(servedDocument));
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

function failedV2Run(failure_code: string) {
  return {
    workflow_format_version: 2,
    run_id: "run-1",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    agent_binding_set_hash: digest,
    agent_bindings: [],
    state_version: 2,
    state: "FAILED",
    current_node: {
      type: "agent",
      node_id: "agent",
      role: "builder",
      job: "work",
      next_node_id: "done"
    },
    node_rail: [
      { node_id: "agent", state: "failed", attempt: { ordinal: 1, state: "FAILED" } },
      { node_id: "done", state: "queued", attempt: null }
    ],
    agent_attempts: [
      {
        attempt_id: digest,
        node_execution_id: digest,
        request_hash: digest,
        attempt_ordinal: 1,
        state: "FAILED",
        failure_code,
        cancellation: null
      }
    ],
    waiting: { type: "NONE" },
    terminal_hash: digest,
    latest_event_cursor: "event1.cnVuLTE.1"
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
          workflow_revision_hash: digest,
          workflow_format_version: 3,
          executable: false,
          not_executable_reason: "agent forms nothing binds yet: outputs",
          name: "Nightly regression sweep",
          description: "Runs the sweep and files what it finds."
        }
      : { workflow_revision_hash: digest };
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
        workflow_revision_hash: digest,
        workflow_format_version: 3,
        executable: false,
        not_executable_reason: "agent forms nothing binds yet: outputs",
        name: "Nightly regression sweep",
        description: "Runs the sweep and files what it finds."
      }
    ]);
  });
});

describe("the catalog name the picker asks for the head", () => {
  it("asks the existing by-name door and decodes the resolution the document serves", async () => {
    const body = {
      display_name: "drei-saetze-review-sehend",
      lineage_id: digest,
      workflow_revision_hash: digest,
      revision_number: 2
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const resolved = await createCockpitApi(fetcher).getRevisionByName(
      "drei-saetze-review-sehend"
    );

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/workflow-revisions/by-name/drei-saetze-review-sehend"
    );
    expect(resolved).toEqual(body);
  });

  it("refuses a catalog head whose display name is not the asked name", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({
        display_name: "another-name",
        lineage_id: digest,
        workflow_revision_hash: digest,
        revision_number: 2
      }), { status: 200, headers: { "content-type": "application/json" } })
    );

    await expect(createCockpitApi(fetcher).getRevisionByName("drei-saetze-review-sehend"))
      .rejects.toThrow(/another display name/);
  });

  it("proves(a-cockpit-published-v3-workflow-is-named-over-the-api): founds a lineage through the existing door", async () => {
    const body = {
      display_name: "diff-review",
      lineage_id: digest,
      workflow_revision_hash: digest,
      revision_number: 1
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 201,
        headers: { "content-type": "application/json" }
      })
    );

    const founded = await createCockpitApi(fetcher).foundCatalogLineage({
      workflow_revision_hash: digest,
      actor: "atelier2-cockpit",
      activated_at: "2026-08-18T07:00:00Z"
    });

    expect(String(fetcher.mock.calls[0]?.[0])).toBe("/atelier/api/v1/workflow-lineages");
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      workflow_revision_hash: digest,
      actor: "atelier2-cockpit",
      activated_at: "2026-08-18T07:00:00Z"
    });
    expect(founded).toEqual({ status: 201, value: body });
  });
});

describe("answering a wait over the existing door", () => {
  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): decodes a V3 run the answers door returns", async () => {
    const run = {
      workflow_format_version: 3,
      run_id: "v3/answer-card",
      public_run_reference: "run1.cnVu",
      workflow_revision_hash: digest,
      agent_binding_set_hash: digest,
      run_configuration_revision_hash: digest,
      agent_bindings: [],
      orders: [],
      state_version: 2,
      state: "COMPLETED",
      current_node_id: "ask",
      node_rail: [{ node_id: "ask", state: "succeeded", attempt: null }],
      cancellation: notCancellableBlock("already-ended"),
      terminal_hash: digest,
      latest_event_cursor: "event1.cnVu.1"
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(run), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
    const mutation = {
      mutation_id: "wait:run1.cnVu:ask",
      kind: "wait" as const,
      target: "/atelier/api/v1/runs/run1.cnVu/answers",
      content_type: "application/json" as const,
      body_base64: btoa(
        JSON.stringify({
          workflow_revision_hash: digest,
          node_id: "ask",
          answer_base64: btoa("true")
        })
      ),
      public_run_reference: "run1.cnVu",
      workflow_revision_hash: digest,
      node_id: "ask",
      answer_base64: btoa("true"),
      answer_hash: digest
    };

    const answered = await createCockpitApi(fetcher).answer(mutation);

    expect(String(fetcher.mock.calls[0]?.[0])).toBe("/atelier/api/v1/runs/run1.cnVu/answers");
    expect(answered).toEqual({ status: 200, value: run });
  });
});

describe("cancelling a run over its cancel door", () => {
  const request = cancelMutation(publicReference, digest, "cancel-key-1");

  function cancellingRun() {
    return {
      workflow_format_version: 3,
      run_id: "v3/cancel",
      public_run_reference: publicReference,
      workflow_revision_hash: digest,
      agent_binding_set_hash: "b".repeat(64),
      run_configuration_revision_hash: "c".repeat(64),
      agent_bindings: [],
      orders: [],
      state_version: 3,
      state: "STARTED",
      current_node_id: "review",
      node_rail: [{ node_id: "review", state: "working", attempt: null }],
      cancellation: notCancellableBlock("already-cancelling"),
      terminal_hash: null,
      latest_event_cursor: "event1.cnVu.3"
    };
  }

  it("posts the exact command to the cancel door and decodes the run it returns", async () => {
    const run = cancellingRun();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(run), {
        status: 202,
        headers: { "content-type": "application/json" }
      })
    );

    const result = await createCockpitApi(fetcher).cancelRun(request);

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      `/atelier/api/v1/runs/${publicReference}/cancellations`
    );
    expect(result).toEqual({ status: 202, value: run });
  });

  it.each([
    "run-not-cancellable",
    "run-cancellation-command-conflict",
    "run-cancellation-overtaken-by-success"
  ] as const)(
    "reads a 409 %s as a definitive refusal carrying its decoded problem, never a retryable one",
    async (code) => {
      const problem = {
        type: `${PROBLEM_TYPE_PREFIX}${code}`,
        title: problemDefinitions[code].title,
        status: 409,
        detail: "The server's own words for why this cancel cannot land."
      };
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(problem), {
          status: 409,
          headers: { "content-type": "application/json" }
        })
      );

      await expect(createCockpitApi(fetcher).cancelRun(request)).rejects.toMatchObject({
        definitive_failure: true,
        problem
      });
    }
  );
});

describe("the published agent-configuration listing", () => {
  it("asks the collection with the house page bound and decodes the item form", async () => {
    const item = {
      model: "sonnet",
      auth_profile_revision_hash: digest,
      executor_revision: "claude-subscription/v1",
      provider_id: "anthropic",
      auth_mode: "subscription",
      requested_capability: "headless",
      agent_configuration_revision_hash: digest,
      startable: true,
      not_startable_reason: null
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [item], next_after_revision_hash: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listAgentConfigurationRevisions();

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/agent-configuration-revisions?limit=50"
    );
    expect(page.items).toEqual([item]);
  });

  it("refuses a list item whose startability and reason disagree", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              model: "sonnet",
              auth_profile_revision_hash: digest,
              executor_revision: "claude-subscription/v1",
              provider_id: "anthropic",
              auth_mode: "subscription",
              requested_capability: "headless",
              agent_configuration_revision_hash: digest,
              startable: false,
              not_startable_reason: null
            }
          ],
          next_after_revision_hash: null
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    await expect(createCockpitApi(fetcher).listAgentConfigurationRevisions()).rejects.toThrow(
      "response did not match the durable wire contract"
    );
  });
});

describe("the observed queue a start-sheet work-item picker reads", () => {
  it("asks the served observed queue page and decodes its cursor", async () => {
    const item = {
      project_id: "atelier",
      tracker_item_reference: "gh:450",
      item_id: digest,
      revision: 0
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [item], next_after: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listObservedQueueItems();

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/observed-queue-items?limit=50"
    );
    expect(page.items).toEqual([item]);
  });
});

describe("the published agent definitions the catalog reads", () => {
  const digest = "a".repeat(64);

  it("asks the listing door for one page", async () => {
    const item = {
      agent_definition_revision_hash: digest,
      name: "scribe",
      description: "Writes what the stage needs."
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [item], next_after_revision_hash: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listAgentDefinitionRevisions();

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/agent-definition-revisions?limit=50"
    );
    expect(page.items).toEqual([item]);
  });

  it("sends the authored file as the exact Markdown bytes the door takes", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ agent_definition_revision_hash: digest }), {
        status: 201,
        headers: { "content-type": "application/json" }
      })
    );
    const authored = "---\nname: scribe\ndescription: Writes.\n---\n\nYou write.\n";

    const result = await createCockpitApi(fetcher).publishAgentDefinition(authored);

    const request = fetcher.mock.calls[0]?.[1];
    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      "/atelier/api/v1/agent-definition-revisions"
    );
    expect(request?.method).toBe("POST");
    expect((request?.headers as Record<string, string>)["content-type"]).toBe(
      "text/markdown"
    );
    expect(new TextDecoder().decode(request?.body as Uint8Array)).toBe(authored);
    expect(result).toEqual({
      status: 201,
      value: { agent_definition_revision_hash: digest }
    });
  });

  it("carries the refusal the door named instead of a sentence of its own", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          type: "urn:atelier2:problem:v1:agent-definition-field-unknown",
          title: "Invalid agent definition document",
          status: 422,
          detail: "agent-definition-field-unknown: color"
        }),
        { status: 422, headers: { "content-type": "application/problem+json" } }
      )
    );

    await expect(
      createCockpitApi(fetcher).publishAgentDefinition("---\ncolor: cyan\n---\nBody.\n")
    ).rejects.toThrow("agent-definition-field-unknown: color");
  });
});

describe("the graph a run is allowed to hold", () => {
  it("refuses a published V3 graph by name instead of reading it as an empty workflow", () => {
    const published = {
      workflow_format_version: 3 as const,
      executable: false as const,
      not_executable_reason: "agent forms nothing binds yet: outputs" as const,
      agent_roles: [],
      orders: [],
      wait_answer_schemas: [],
      node_count: 1,
      node_previews: [
        {
          id: "only",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Sweep the suite.",
          depends_on: []
        }
      ],
      loops: [],
      name: "Nightly regression sweep",
      description: null
    };

    expect(() => executableGraph(published)).toThrow(
      "a run cannot hold a revision no run can start"
    );
  });

  it("hands back an executable graph unchanged", () => {
    const graph = executableGraph(workflowRevision().graph);

    expect(graph.workflow_format_version).toBe(1);
    expect(executableGraph(graph)).toBe(graph);
  });
});

describe("the run listing the studio opens on", () => {
  const v3Run = {
    workflow_format_version: 3,
    run_id: "run-1",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "implement",
    node_rail: [{ node_id: "implement", state: "working", attempt: null }],
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null
  };

  it("proves(the-run-listing-holds-every-format-the-api-answers-with): decodes a page that holds a version 3 run instead of failing the whole studio", async () => {
    // The operator's own repro: one V3 run exists, and every level that lists
    // runs -- the studio and the project -- answered "Request failed — wire
    // contract" because the page decoder knew only V1 and V2. The detail page
    // had been taught V3; the listing had not, so a single V3 run took down the
    // page the workshop opens on.
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [v3Run], next_after: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listRuns();

    expect(fetcher.mock.calls[0]?.[0]).toBe("/atelier/api/v1/runs?limit=50");
    expect(page.items).toHaveLength(1);
    expect(page.items[0]?.public_run_reference).toBe(publicReference);
    expect(page.items[0]?.state).toBe("STARTED");
  });

  it("decodes a run started with an order, its size and pinned schema, never its bytes", async () => {
    const orderedRun = {
      ...v3Run,
      run_id: "run-with-an-order",
      orders: [
        {
          name: "headline",
          bytes: 19,
          schema_revision_hash: "d".repeat(64)
        }
      ]
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [orderedRun], next_after: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const page = await createCockpitApi(fetcher).listRuns();

    expect(page.items).toHaveLength(1);
    const run = page.items[0];
    if (!run || !isRunV3(run)) {
      throw new Error("expected a decoded V3 run");
    }
    expect(run.orders).toEqual([
      { name: "headline", bytes: 19, schema_revision_hash: "d".repeat(64) }
    ]);
  });

  it("still lists a version 2 run beside it", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ items: [v3Run, startedRun()], next_after: null }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );

    const page = await createCockpitApi(fetcher).listRuns();

    expect(page.items.map((run) => run.state)).toEqual(["STARTED", "STARTED"]);
  });

  it("asks the list for one durable state when the studio names that state", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [], next_after: null }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    await createCockpitApi(fetcher).listRuns(undefined, "WAITING_INPUT");

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      "/atelier/api/v1/runs?limit=50&state=WAITING_INPUT"
    );
  });
});

describe("the project listing the picker will consume", () => {
  it("asks the zero-or-one project door and refuses fields the server did not declare", async () => {
    const project = { public_project_reference: "project1.dGVhbS9yZWQ" };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [project] }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const listed = await createCockpitApi(fetcher).listProjects();

    expect(fetcher.mock.calls[0]?.[0]).toBe("/atelier/api/v1/projects");
    expect(listed.items).toEqual([project]);

    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [{ ...project, project_id: "team/red" }] }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
    await expect(createCockpitApi(fetcher).listProjects()).rejects.toThrow(
      "durable wire contract"
    );
  });
});

describe("the project source connection Settings will read", () => {
  const projectReference = "project1.dGVhbS9yZWQ";
  const connection = {
    public_project_reference: projectReference,
    revision_number: 3,
    source_kind: "github",
    source_address: "FlexOr2/atelier-2",
    auth_method: "personal-access-token" as const,
    project_source_connection_revision_hash: "a".repeat(64)
  };

  it("asks the source-connection door and decodes only its declared resource", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(connection), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const read = await createCockpitApi(fetcher).getProjectSourceConnection(projectReference);

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `/atelier/api/v1/projects/${projectReference}/source-connection`
    );
    expect(read).toEqual(projectSourceConnectionRevisionSchema.parse(connection));
  });

  it("refuses extra fields and a response for another project", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...connection, credential_directory: "/operator/credentials" }), {
          status: 200,
          headers: { "content-type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ ...connection, public_project_reference: "project1.b3RoZXI" }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
      );
    const client = createCockpitApi(fetcher);

    await expect(client.getProjectSourceConnection(projectReference)).rejects.toThrow(
      "durable wire contract"
    );
    await expect(client.getProjectSourceConnection(projectReference)).rejects.toThrow(
      /another project/
    );
  });
});

describe("the project model configuration the start sheet consumes", () => {
  const projectReference = "project1.dGVhbS9yZWQ";
  const configurationHash = "d".repeat(64);
  const registry = {
    provider_id: "openai",
    revision_number: 1,
    model_registry_revision_hash: "a".repeat(64),
    entries: [{
      model_id: "gpt-5.6",
      agent_configuration_revision_hash: configurationHash,
      source: "discovered",
      provider_check: "checked"
    }]
  };

  it("reads a provider registry and resolves project roles", async () => {
    const resolution = {
      project_id: "team/red",
      public_project_reference: projectReference,
      workflow_revision_hash: "b".repeat(64),
      resolutions: [{
        role: "builder",
        agent_configuration_revision_hash: configurationHash,
        source: "pinned-in-workflow",
        model_id: "gpt-5.6",
        declared_difficulty: 2,
        default_difficulty: 2,
        uncast_reason: null,
        family_differs_from: null
      }]
    };
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(registry), { status: 200, headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(resolution), { status: 200, headers: { "content-type": "application/json" } }));
    const client = createCockpitApi(fetcher);

    expect((await client.getModelRegistry("openai")).entries[0]?.model_id).toBe("gpt-5.6");
    expect((await client.resolveProjectModels(projectReference, "b".repeat(64), [])).resolutions[0]?.source).toBe("pinned-in-workflow");
    expect(fetcher.mock.calls[1]?.[1]?.body).toBe(JSON.stringify({ workflow_revision_hash: "b".repeat(64), overrides: [] }));
  });

  it.each([
    {
      name: "a registry naming another provider",
      response: { ...registry, provider_id: "anthropic" },
      read: (client: ReturnType<typeof createCockpitApi>) => client.getModelRegistry("openai"),
      message: "model registry response named another provider"
    },
    {
      name: "a resolution naming another project",
      response: {
        project_id: "team/blue",
        public_project_reference: "project1.dGVhbS9ibHVl",
        workflow_revision_hash: "b".repeat(64),
        resolutions: []
      },
      read: (client: ReturnType<typeof createCockpitApi>) =>
        client.resolveProjectModels(projectReference, "b".repeat(64), []),
      message: "model resolution response named another project"
    },
    {
      name: "a resolution naming another workflow",
      response: {
        project_id: "team/red",
        public_project_reference: projectReference,
        workflow_revision_hash: "c".repeat(64),
        resolutions: []
      },
      read: (client: ReturnType<typeof createCockpitApi>) =>
        client.resolveProjectModels(projectReference, "b".repeat(64), []),
      message: "model resolution response named another workflow"
    }
  ])("refuses $name", async ({ response, read, message }) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(response), { status: 200, headers: { "content-type": "application/json" } })
    );

    await expect(read(createCockpitApi(fetcher))).rejects.toThrow(message);
  });
});

describe("the node a click asks the server about", () => {
  const nodeDetail = {
    run_id: "run-1",
    public_run_reference: publicReference,
    node_id: "review",
    state: "queued",
    job_base64: null,
    job_hash: null,
    answer: null,
    provenance: null,
    refusal: null
  };

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): asks the node route and decodes the answer", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(nodeDetail), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const detail = await createCockpitApi(fetcher).getNodeDetail(publicReference, "review");

    expect(String(fetcher.mock.calls[0]?.[0])).toBe(
      `/atelier/api/v1/runs/${publicReference}/nodes/review`
    );
    expect(detail.node_id).toBe("review");
    expect(detail.refusal).toBeNull();
  });

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): decodes a node that ran, with its job, its answer and its provenance", async () => {
    const ran = {
      ...nodeDetail,
      node_id: "implement",
      state: "succeeded",
      job_base64: btoa("Write three German sentences."),
      job_hash: digest,
      answer: { value_base64: btoa("Ein gutes Review."), value_hash: "d".repeat(64) },
      provenance: {
        role: "builder",
        provider_id: "anthropic",
        model: "sonnet",
        executor_revision: "headless-print-json/v1",
        executor_operational_identity: "headless-print-json/v1",
        auth_mode: "subscription",
        profile_id: "operator-subscription",
        agent_configuration_revision_hash: "e".repeat(64),
        request_hash: "f".repeat(64),
        receipt_hash: "a".repeat(64)
      }
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(ran), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    const detail = await createCockpitApi(fetcher).getNodeDetail(publicReference, "implement");

    expect(detail.provenance?.model).toBe("sonnet");
    expect(detail.answer?.value_hash).toBe("d".repeat(64));
    // The two hashes are different values, and the decoder keeps them apart.
    expect(detail.job_hash).not.toBe(detail.provenance?.request_hash);
  });

  it("refuses an answer that names another node instead of showing it", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ...nodeDetail, node_id: "somewhere-else" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    await expect(
      createCockpitApi(fetcher).getNodeDetail(publicReference, "review")
    ).rejects.toThrow(/named another node/);
  });
});
