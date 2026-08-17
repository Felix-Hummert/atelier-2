import { z } from "zod";

import type {
  PublishMutation,
  ReconciliationMutation,
  StartMutation,
  WaitMutation
} from "../lib/mutationJournal";

const sha256 = z.string().regex(/^[0-9a-f]{64}$/);
const standardBase64 = z.string().refine(isCanonicalStandardBase64, "base64 must use the canonical standard alphabet and padding");
const publicRunReference = z.string().refine(
  (value) => decodePublicRunReference(value) !== null,
  "public run reference must contain canonical unpadded base64url UTF-8"
);
const eventCursor = z.string().refine(
  (value) => parseEventCursor(value) !== null,
  "event cursor must contain a canonical run reference and safe positive sequence"
);
const safeInteger = z.number().refine(Number.isSafeInteger, "integer must be exactly representable");
const nonnegativeSafeInteger = safeInteger.refine((value) => value >= 0);
const positiveSafeInteger = safeInteger.refine((value) => value > 0);

const agentNodeV1Schema = z
  .object({
    type: z.literal("agent"),
    node_id: z.string().min(1),
    job: z.string().min(1),
    output: z.string().min(1),
    next_node_id: z.string().min(1)
  })
  .strict();

const agentNodeV2Schema = z
  .object({
    type: z.literal("agent"),
    node_id: z.string().min(1),
    role: z.string().min(1),
    job: z.string().min(1),
    next_node_id: z.string().min(1)
  })
  .strict();

const actionNodeSchema = z
  .object({
    type: z.literal("action"),
    node_id: z.string().min(1),
    next_node_id: z.string().min(1)
  })
  .strict();

const waitNodeSchema = z
  .object({
    type: z.literal("wait"),
    node_id: z.string().min(1),
    answer_type: z.literal("integer"),
    next_node_id: z.string().min(1)
  })
  .strict();

const subworkflowNodeSchema = z
  .object({
    type: z.literal("subworkflow"),
    node_id: z.string().min(1),
    operation: z.literal("add"),
    operands: z.tuple([safeInteger, safeInteger]),
    next_node_id: z.null()
  })
  .strict();

export const nodeSchema = z.discriminatedUnion("type", [
  agentNodeV1Schema,
  actionNodeSchema,
  waitNodeSchema,
  subworkflowNodeSchema
]);

const nodeV2Schema = z.discriminatedUnion("type", [
  agentNodeV2Schema,
  actionNodeSchema,
  waitNodeSchema,
  subworkflowNodeSchema
]);

const workflowGraphV1Schema = z
  .object({
    format_version: z.literal(1),
    start_node_id: z.string().min(1),
    nodes: z.array(nodeSchema)
  })
  .strict()
  .superRefine(validateWorkflowGraph);

const workflowGraphV2Schema = z
  .object({
    format_version: z.literal(2),
    start_node_id: z.string().min(1),
    nodes: z.array(nodeV2Schema)
  })
  .strict()
  .superRefine(validateWorkflowGraph);

/**
 * A published V3 revision says what it is and whether this build runs it. It
 * carries no nodes to walk, so there is no graph to validate here -- only the
 * truth the operator has to be told instead of a generic wire-contract failure.
 *
 * `executable` was pinned to `false` here, which mirrored a server that could
 * never answer otherwise. It can now, and a literal would have turned the first
 * honest `true` into exactly the wire-contract failure this shape exists to
 * prevent. `not_executable_reason` carries the server's own words about which
 * form is still waiting, so the operator is told about the document rather than
 * about version 3.
 */
const workflowGraphV3Schema = z
  .object({
    format_version: z.literal(3),
    executable: z.boolean(),
    not_executable_reason: z.string().nullable(),
    node_count: z.number().int().positive(),
    name: z.string().min(1),
    description: z.string().nullable()
  })
  .strict();

const workflowGraphSchema = z.discriminatedUnion("format_version", [
  workflowGraphV1Schema,
  workflowGraphV2Schema,
  workflowGraphV3Schema
]);

function validateWorkflowGraph(
  graph: {
    start_node_id: string;
    nodes: Array<z.infer<typeof nodeSchema> | z.infer<typeof nodeV2Schema>>;
  },
  context: z.RefinementCtx
): void {
    const byId = new Map(graph.nodes.map((node) => [node.node_id, node]));
    if (graph.nodes.length === 0 || byId.size !== graph.nodes.length) {
      context.addIssue({ code: "custom", message: "workflow nodes must be nonempty and unique" });
      return;
    }
    const start = byId.get(graph.start_node_id);
    if (start === undefined || start.type === "action") {
      context.addIssue({ code: "custom", message: "workflow start is missing or invalid" });
      return;
    }
    const terminalCount = graph.nodes.filter((node) => node.type === "subworkflow").length;
    const actions = graph.nodes.filter((node) => node.type === "action");
    if (terminalCount !== 1 || actions.length > 1) {
      context.addIssue({ code: "custom", message: "workflow terminal or Action count is invalid" });
      return;
    }
    const predecessors = new Map<string, typeof graph.nodes>();
    for (const node of graph.nodes) predecessors.set(node.node_id, []);
    for (const node of graph.nodes) {
      if (node.next_node_id === null) continue;
      const successor = byId.get(node.next_node_id);
      if (successor === undefined) {
        context.addIssue({ code: "custom", message: "workflow successor is missing" });
        return;
      }
      predecessors.get(successor.node_id)?.push(node);
    }
    for (const action of actions) {
      const incoming = predecessors.get(action.node_id) ?? [];
      if (incoming.length !== 1 || incoming[0]?.type !== "agent") {
        context.addIssue({ code: "custom", message: "Action requires one Agent predecessor" });
        return;
      }
    }
    const visited = new Set<string>();
    let current: z.infer<typeof nodeSchema> | z.infer<typeof nodeV2Schema> | undefined = start;
    while (current !== undefined) {
      if (visited.has(current.node_id)) {
        context.addIssue({ code: "custom", message: "workflow graph contains a cycle" });
        return;
      }
      visited.add(current.node_id);
      current = current.next_node_id === null ? undefined : byId.get(current.next_node_id);
    }
    if (visited.size !== graph.nodes.length) {
      context.addIssue({ code: "custom", message: "workflow graph contains unreachable nodes" });
    }
}

/**
 * The cockpit asks for the described listing, because a picker has to offer a
 * name rather than a hash. These fields mirror `WorkflowRevisionSummaryResourceV2`
 * in the frozen document and `servedVocabulary.test.ts` holds them to it: the
 * decoder is `.strict()`, so a field the server adds without this file throws on
 * every load instead of being quietly ignored.
 */
export const workflowRevisionSummarySchema = z
  .object({
    revision_hash: sha256,
    format_version: z.union([z.literal(1), z.literal(2), z.literal(3)]),
    executable: z.boolean(),
    not_executable_reason: z.string().nullable(),
    name: z.string().nullable(),
    description: z.string().nullable()
  })
  .strict();

export const workflowRevisionDetailSchema = z
  .object({
    revision_hash: sha256,
    document_base64: standardBase64,
    graph: workflowGraphSchema
  })
  .strict();

export const workflowRevisionPageSchema = z
  .object({
    items: z.array(workflowRevisionSummarySchema),
    next_after_revision_hash: sha256.nullable()
  })
  .strict();

const authProfileInputSchema = z
  .object({
    profile_id: z.string().min(1).max(1_024),
    revision_number: positiveSafeInteger,
    provider_id: z.string().min(1).max(64),
    auth_mode: z.enum(["subscription", "api_key"])
  })
  .strict();

const authProfileRevisionSchema = authProfileInputSchema
  .extend({ auth_profile_revision_hash: sha256 })
  .strict();

const agentConfigurationInputSchema = z
  .object({
    model: z.string().min(1).max(1_024),
    auth_profile_revision_hash: sha256,
    executor_revision: z.string().min(1).max(1_024),
    requested_capability: z.enum(["headless", "interactive"]).optional()
  })
  .strict();

const agentConfigurationRevisionSchema = agentConfigurationInputSchema
  .extend({
    provider_id: z.string().min(1).max(64),
    auth_mode: z.enum(["subscription", "api_key"]),
    requested_capability: z.enum(["headless", "interactive"]),
    agent_configuration_revision_hash: sha256
  })
  .strict();

const operatorFoundSchema = z
  .object({
    type: z.literal("operator_found"),
    effect_id: z.string().min(1),
    result_base64: standardBase64
  })
  .strict();

const authoritativeAbsenceSchema = z
  .object({ type: z.literal("operator_authoritative_absence") })
  .strict();

export const reconciliationDeterminationSchema = z.discriminatedUnion("type", [
  operatorFoundSchema,
  authoritativeAbsenceSchema
]);

const reconciliationCommandSchema = z
  .object({
    command_id: z.string().min(1),
    actor: z.string().min(1),
    evidence: z.string().min(1),
    state: z.literal("PENDING"),
    determination: reconciliationDeterminationSchema
  })
  .strict();

const noWaitingSchema = z.object({ type: z.literal("NONE") }).strict();
const waitingInputSchema = z
  .object({
    type: z.literal("WAITING_INPUT"),
    node_id: z.string().min(1),
    answer_type: z.literal("integer")
  })
  .strict();
const waitingReconciliationSchema = z
  .object({
    type: z.literal("WAITING_RECONCILIATION"),
    node_id: z.string().min(1),
    logical_effect_key: z.string().min(1),
    request_hash: sha256,
    request_base64: standardBase64,
    intent_state_version: nonnegativeSafeInteger,
    pending_command: reconciliationCommandSchema.nullable()
  })
  .strict();
const waitingSchema = z.discriminatedUnion("type", [
  noWaitingSchema,
  waitingInputSchema,
  waitingReconciliationSchema
]);

const runV1Schema = z
  .object({
    run_id: z.string().min(1),
    public_run_reference: publicRunReference,
    workflow_revision_hash: sha256,
    state_version: nonnegativeSafeInteger,
    state: z.enum(["STARTED", "WAITING_RECONCILIATION", "WAITING_INPUT", "COMPLETED"]),
    current_node: nodeSchema,
    waiting: waitingSchema,
    terminal_hash: sha256.nullable(),
    latest_event_cursor: eventCursor.nullable()
  })
  .strict()
  .superRefine(validateRunShape);

const agentBindingV2Schema = z
  .object({
    role: z.string().min(1),
    agent_configuration_revision_hash: sha256,
    auth_profile_revision_hash: sha256,
    profile_id: z.string().min(1),
    revision_number: positiveSafeInteger,
    provider_id: z.string().min(1),
    auth_mode: z.enum(["subscription", "api_key"]),
    model: z.string().min(1),
    executor_revision: z.string().min(1)
  })
  .strict();

const attemptCancellationV2Schema = z
  .object({
    command_id: z.string().min(1),
    replacement: z.enum(["NONE", "ONE"]),
    redrive_state: z.enum(["PENDING", "OWNER_NOT_LOCAL", "CLEANUP_ATTESTED"]),
    disposition: z
      .enum([
        "NEVER_LAUNCHED",
        "EXITED_BEFORE_SIGNAL",
        "REAPED_AFTER_TERM",
        "REAPED_AFTER_KILL",
        "OWNER_LOST_AFTER_PARENT_DEATH"
      ])
      .nullable()
  })
  .strict()
  .superRefine((cancellation, context) => {
    if ((cancellation.redrive_state === "CLEANUP_ATTESTED") !== (cancellation.disposition !== null)) {
      context.addIssue({ code: "custom", message: "cleanup attestation and disposition disagree" });
    }
  });

/** The attempt states the served document names, in the order it names them. */
export const PUBLIC_ATTEMPT_STATES = [
  "PREPARED",
  "POSSIBLY_RAN",
  "CANCEL_REQUESTED",
  "CANCELLED",
  "INTERRUPTED",
  "FAILED"
] as const;

const agentAttemptV2Schema = z
  .object({
    attempt_id: sha256,
    node_execution_id: sha256,
    request_hash: sha256,
    attempt_ordinal: z.union([z.literal(1), z.literal(2)]),
    state: z.enum(PUBLIC_ATTEMPT_STATES),
    failure_code: z.literal("PROCESS_EXITED_UNSUCCESSFULLY").nullable(),
    cancellation: attemptCancellationV2Schema.nullable()
  })
  .strict()
  .superRefine((attempt, context) => {
    if ((attempt.state === "FAILED") !== (attempt.failure_code !== null)) {
      context.addIssue({ code: "custom", message: "agent attempt state and failure code disagree" });
    }
    const cancellationState = ["CANCEL_REQUESTED", "CANCELLED", "INTERRUPTED"].includes(
      attempt.state
    );
    if (cancellationState !== (attempt.cancellation !== null)) {
      context.addIssue({ code: "custom", message: "agent attempt state and cancellation disagree" });
    }
  });

/** The states the served document names; tests/api/servedVocabulary holds them to it. */
export const NODE_STATES = [
  "queued",
  "working",
  "needs_you",
  "succeeded",
  "failed",
  "cancelled",
  "interrupted"
] as const;

const nodeRailEntrySchema = z
  .object({
    node_id: z.string().min(1),
    state: z.enum(NODE_STATES),
    attempt: z
      .object({
        ordinal: z.union([z.literal(1), z.literal(2)]),
        state: z.enum(PUBLIC_ATTEMPT_STATES).nullable()
      })
      .strict()
      .nullable()
  })
  .strict();

const runV2Schema = z
  .object({
    workflow_format_version: z.literal(2),
    run_id: z.string().min(1),
    public_run_reference: publicRunReference,
    workflow_revision_hash: sha256,
    agent_binding_set_hash: sha256,
    agent_bindings: z.array(agentBindingV2Schema).max(100),
    state_version: nonnegativeSafeInteger,
    state: z.enum(["STARTED", "WAITING_RECONCILIATION", "WAITING_INPUT", "COMPLETED"]),
    current_node: nodeV2Schema,
    node_rail: z.array(nodeRailEntrySchema).min(1),
    agent_attempts: z.array(agentAttemptV2Schema).max(2),
    waiting: waitingSchema,
    terminal_hash: sha256.nullable(),
    latest_event_cursor: eventCursor.nullable()
  })
  .strict()
  .superRefine(validateRunShape);

/**
 * A V3 run as the server answers it, decoded on its own rather than folded into
 * the run union above.
 *
 * The two shapes disagree about what a run has. A V3 run names its current node
 * by id and carries no `current_node` object, no `waiting` and no
 * `agent_attempts`, because no runtime interprets a Wait or Action node in that
 * format yet. Widening `runSchema` would push a "this format has no such thing"
 * guard onto every reader of those fields; keeping it separate leaves the V2
 * cockpit exactly as it was and lets the V3 view render what actually exists.
 */
const runV3Schema = z
  .object({
    workflow_format_version: z.literal(3),
    run_id: z.string().min(1),
    public_run_reference: publicRunReference,
    workflow_revision_hash: sha256,
    agent_binding_set_hash: sha256,
    run_configuration_revision_hash: sha256,
    agent_bindings: z.array(agentBindingV2Schema).max(100),
    state_version: nonnegativeSafeInteger,
    state: z.enum(["STARTED", "COMPLETED"]),
    current_node_id: z.string().min(1),
    node_rail: z.array(nodeRailEntrySchema).min(1),
    terminal_hash: sha256.nullable(),
    latest_event_cursor: eventCursor.nullable()
  })
  .strict();

export const runSchema = z.union([runV2Schema, runV1Schema]);

/**
 * Whether this run is a version 3 one.
 *
 * A plain `run.workflow_format_version === 3` cannot narrow the union: a version
 * 1 run carries no format field at all, so the property has to be asked for
 * before it is compared.
 */
export function isRunV3(run: AnyRun): run is RunV3 {
  return "workflow_format_version" in run && run.workflow_format_version === 3;
}

/** Every run the server can answer with, before a reader narrows it by format. */
export const anyRunSchema = z.union([runV3Schema, runV2Schema, runV1Schema]);

function validateRunShape(
  run: {
    run_id: string;
    public_run_reference: string;
    latest_event_cursor: string | null;
    state: "STARTED" | "WAITING_RECONCILIATION" | "WAITING_INPUT" | "COMPLETED";
    current_node: z.infer<typeof nodeSchema> | z.infer<typeof nodeV2Schema>;
    waiting: z.infer<typeof waitingSchema>;
    terminal_hash: string | null;
  },
  context: z.RefinementCtx
): void {
    const referencedRunId = decodePublicRunReference(run.public_run_reference);
    if (referencedRunId !== run.run_id) {
      context.addIssue({ code: "custom", message: "run id and public reference disagree" });
    }
    if (run.latest_event_cursor !== null) {
      const parsedCursor = parseEventCursor(run.latest_event_cursor);
      if (parsedCursor?.publicRunReference !== run.public_run_reference) {
        context.addIssue({ code: "custom", message: "latest cursor belongs to another run" });
      }
    }
    const valid =
      (run.state === "STARTED" && run.waiting.type === "NONE" && run.terminal_hash === null) ||
      (run.state === "WAITING_INPUT" &&
        run.current_node.type === "wait" &&
        run.waiting.type === "WAITING_INPUT" &&
        run.current_node.node_id === run.waiting.node_id &&
        run.terminal_hash === null) ||
      (run.state === "WAITING_RECONCILIATION" &&
        run.current_node.type === "action" &&
        run.waiting.type === "WAITING_RECONCILIATION" &&
        run.current_node.node_id === run.waiting.node_id &&
        run.terminal_hash === null) ||
      (run.state === "COMPLETED" &&
        run.current_node.type === "subworkflow" &&
        run.waiting.type === "NONE" &&
        run.terminal_hash !== null);
    if (!valid) {
      context.addIssue({ code: "custom", message: "run state fields disagree" });
    }
}

/**
 * The list answers every run format the server can project. Mutation
 * responses stay on `runSchema` so V3 is not folded into the V1/V2 union
 * those readers already assume.
 */
export const runPageSchema = z
  .object({ items: z.array(anyRunSchema), next_after: publicRunReference.nullable() })
  .strict();

const receiptSchema = z
  .object({
    logical_effect_key: z.string().min(1),
    request_hash: sha256,
    effect_id: z.string().min(1),
    result_hash: sha256,
    result_base64: standardBase64,
    confirmation_source: z.enum([
      "ADAPTER_READBACK",
      "ADAPTER_EXECUTION",
      "OPERATOR_FOUND",
      "OPERATOR_AUTHORIZED_EXECUTION"
    ]),
    reconcile_command_id: z.string().min(1).nullable()
  })
  .strict();

const eventBase = {
  cursor: eventCursor,
  sequence: positiveSafeInteger,
  public_run_reference: publicRunReference,
  workflow_revision_hash: sha256,
  node_id: z.string().min(1),
  node_execution_id: sha256,
  event_hash: sha256
};

const v2EventBase = {
  workflow_format_version: z.literal(2),
  ...eventBase,
  node_rail: z.array(nodeRailEntrySchema).min(1)
};
const v2AttemptEvent = {
  attempt_id: sha256,
  attempt_ordinal: z.union([z.literal(1), z.literal(2)])
};
const v2CancellationEvent = {
  ...v2AttemptEvent,
  command_id: z.string().min(1).max(1_024),
  replacement: z.enum(["NONE", "ONE"])
};
const v2Disposition = z.enum([
  "NEVER_LAUNCHED",
  "EXITED_BEFORE_SIGNAL",
  "REAPED_AFTER_TERM",
  "REAPED_AFTER_KILL",
  "OWNER_LOST_AFTER_PARENT_DEATH"
]);

const runEventV1Schema = z
  .discriminatedUnion("event", [
    z.object({ ...eventBase, event: z.literal("AGENT_COMPLETED"), output: z.string(), payload_hash: sha256 }).strict(),
    z.object({ ...eventBase, event: z.literal("ACTION_RECONCILIATION_REQUIRED"), request_base64: standardBase64, request_hash: sha256 }).strict(),
    z.object({ ...eventBase, event: z.literal("ACTION_RECONCILIATION_RESOLVED"), receipt: receiptSchema }).strict(),
    z.object({ ...eventBase, event: z.literal("ACTION_COMPLETED"), receipt: receiptSchema }).strict(),
    z.object({ ...eventBase, event: z.literal("WAITING_INPUT"), answer_type: z.literal("integer") }).strict(),
    z.object({ ...eventBase, event: z.literal("WAIT_ANSWERED"), answer: z.string().regex(/^(?:0|-?[1-9][0-9]*)$/), answer_hash: sha256 }).strict(),
    z.object({ ...eventBase, event: z.literal("SUBWORKFLOW_COMPLETED"), result: safeInteger, result_hash: sha256 }).strict()
  ])
  .superRefine(validateEventCursor);

const runEventV2Schema = z
  .discriminatedUnion("event", [
    z.object({ ...v2EventBase, ...v2AttemptEvent, event: z.literal("AGENT_COMPLETED"), output_base64: standardBase64, output_hash: sha256 }).strict(),
    z.object({ ...v2EventBase, ...v2AttemptEvent, event: z.literal("AGENT_FAILED"), failure_code: z.literal("PROCESS_EXITED_UNSUCCESSFULLY") }).strict(),
    z.object({ ...v2EventBase, ...v2CancellationEvent, event: z.literal("AGENT_CANCEL_REQUESTED") }).strict(),
    z.object({ ...v2EventBase, ...v2CancellationEvent, event: z.literal("AGENT_CANCELLED"), disposition: v2Disposition, replacement_attempt_id: sha256.nullable() }).strict(),
    z.object({ ...v2EventBase, ...v2CancellationEvent, event: z.literal("AGENT_INTERRUPTED"), disposition: v2Disposition, replacement_attempt_id: sha256.nullable() }).strict(),
    z.object({ ...v2EventBase, event: z.literal("ACTION_RECONCILIATION_REQUIRED"), request_base64: standardBase64, request_hash: sha256 }).strict(),
    z.object({ ...v2EventBase, event: z.literal("ACTION_RECONCILIATION_RESOLVED"), receipt: receiptSchema }).strict(),
    z.object({ ...v2EventBase, event: z.literal("ACTION_COMPLETED"), receipt: receiptSchema }).strict(),
    z.object({ ...v2EventBase, event: z.literal("WAITING_INPUT"), answer_type: z.literal("integer") }).strict(),
    z.object({ ...v2EventBase, event: z.literal("WAIT_ANSWERED"), answer: z.string().regex(/^(?:0|-?[1-9][0-9]*)$/), answer_hash: sha256 }).strict(),
    z.object({ ...v2EventBase, event: z.literal("SUBWORKFLOW_COMPLETED"), result: safeInteger, result_hash: sha256 }).strict()
  ])
  .superRefine(validateEventCursor);

export const runEventSchema = z.union([runEventV2Schema, runEventV1Schema]);

function validateEventCursor(
  event: { cursor: string; public_run_reference: string; sequence: number },
  context: z.RefinementCtx
): void {
    const parsedCursor = parseEventCursor(event.cursor);
    if (
      parsedCursor?.publicRunReference !== event.public_run_reference ||
      parsedCursor.sequence !== event.sequence
    ) {
      context.addIssue({
        code: "custom",
        message: "event cursor, run reference, and sequence disagree"
      });
    }
}

export const problemDefinitions = {
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
  "reconciliation-determination-conflict": { status: 409, title: "Reconciliation determination conflict" },
  "reconciliation-rejected": { status: 409, title: "Reconciliation was rejected" },
  "route-not-found": { status: 404, title: "Route not found" },
  "method-not-allowed": { status: 405, title: "Method not allowed" },
  "temporarily-unavailable": { status: 503, title: "Temporarily unavailable" },
  "durable-state-corrupt": { status: 500, title: "Durable state is corrupt" },
  "internal-error": { status: 500, title: "Internal error" }
} as const;

export const problemSchema = z.discriminatedUnion("type", [
  problemVariant("auth-profile-revision-conflict", problemDefinitions["auth-profile-revision-conflict"]),
  problemVariant("auth-profile-revision-collision", problemDefinitions["auth-profile-revision-collision"]),
  problemVariant("auth-profile-revision-not-found", problemDefinitions["auth-profile-revision-not-found"]),
  problemVariant("agent-executor-binding-unavailable", problemDefinitions["agent-executor-binding-unavailable"]),
  problemVariant("agent-configuration-revision-collision", problemDefinitions["agent-configuration-revision-collision"]),
  problemVariant("agent-configuration-revision-not-found", problemDefinitions["agent-configuration-revision-not-found"]),
  problemVariant("invalid-agent-bindings", problemDefinitions["invalid-agent-bindings"]),
  problemVariant("invalid-public-run-reference", problemDefinitions["invalid-public-run-reference"]),
  problemVariant("invalid-event-cursor", problemDefinitions["invalid-event-cursor"]),
  problemVariant("invalid-revision-hash", problemDefinitions["invalid-revision-hash"]),
  problemVariant("event-cursor-run-mismatch", problemDefinitions["event-cursor-run-mismatch"]),
  problemVariant("event-cursor-ahead", problemDefinitions["event-cursor-ahead"]),
  problemVariant("invalid-request", problemDefinitions["invalid-request"]),
  problemVariant("invalid-base64", problemDefinitions["invalid-base64"]),
  problemVariant("invalid-workflow-document", problemDefinitions["invalid-workflow-document"]),
  problemVariant("unsupported-media-type", problemDefinitions["unsupported-media-type"]),
  problemVariant("not-acceptable", problemDefinitions["not-acceptable"]),
  problemVariant("workflow-revision-not-found", problemDefinitions["workflow-revision-not-found"]),
  problemVariant("run-not-found", problemDefinitions["run-not-found"]),
  problemVariant("node-not-found", problemDefinitions["node-not-found"]),
  problemVariant("revision-collision", problemDefinitions["revision-collision"]),
  problemVariant("run-identity-conflict", problemDefinitions["run-identity-conflict"]),
  problemVariant("answer-revision-conflict", problemDefinitions["answer-revision-conflict"]),
  problemVariant("answer-state-conflict", problemDefinitions["answer-state-conflict"]),
  problemVariant("answer-bytes-conflict", problemDefinitions["answer-bytes-conflict"]),
  problemVariant("reconciliation-target-missing", problemDefinitions["reconciliation-target-missing"]),
  problemVariant("reconciliation-stale", problemDefinitions["reconciliation-stale"]),
  problemVariant("reconciliation-command-conflict", problemDefinitions["reconciliation-command-conflict"]),
  problemVariant("reconciliation-determination-conflict", problemDefinitions["reconciliation-determination-conflict"]),
  problemVariant("reconciliation-rejected", problemDefinitions["reconciliation-rejected"]),
  problemVariant("route-not-found", problemDefinitions["route-not-found"]),
  problemVariant("method-not-allowed", problemDefinitions["method-not-allowed"]),
  problemVariant("temporarily-unavailable", problemDefinitions["temporarily-unavailable"]),
  problemVariant("durable-state-corrupt", problemDefinitions["durable-state-corrupt"]),
  problemVariant("internal-error", problemDefinitions["internal-error"])
]);

const streamFailureSchema = z
  .object({ event: z.literal("STREAM_FAILED"), problem: problemSchema })
  .strict();

const streamFrameSchema = z.union([streamFailureSchema, runEventSchema]);

export type Problem = z.infer<typeof problemSchema>;
export type StreamFailure = z.infer<typeof streamFailureSchema>;
export type StreamFrame = z.infer<typeof streamFrameSchema>;
export type Run = z.infer<typeof runSchema>;
export type RunV1 = z.infer<typeof runV1Schema>;
export type RunV2 = z.infer<typeof runV2Schema>;
export type RunV3 = z.infer<typeof runV3Schema>;
export type AnyRun = z.infer<typeof anyRunSchema>;
export type RunEvent = z.infer<typeof runEventSchema>;
export type WorkflowGraph = z.infer<typeof workflowGraphSchema>;
export type WorkflowNode = z.infer<typeof nodeSchema> | z.infer<typeof nodeV2Schema>;
export type WorkflowRevisionDetail = z.infer<typeof workflowRevisionDetailSchema>;
export type RunPage = z.infer<typeof runPageSchema>;
export type WorkflowRevisionPage = z.infer<typeof workflowRevisionPageSchema>;
export type WorkflowRevisionSummary = z.infer<typeof workflowRevisionSummarySchema>;

/**
 * The graph of a revision a run can hold. Only an executable format carries
 * nodes to walk, so everything that walks them asks for this rather than
 * re-deciding what a published V3 revision does not have.
 */
export type ExecutableWorkflowGraph = Extract<
  WorkflowRevisionDetail["graph"],
  { start_node_id: string }
>;

/**
 * Narrow a revision's graph to the one a run can hold.
 *
 * The refusal is an impossibility guard, not a reason an operator is meant to
 * read. A run cannot exist on a non-executable revision: the durable starter
 * parses the named revision with the executable parser and answers
 * `DurableRunFormatNotExecutable` before any run is written, so by the time a
 * cockpit holds a run, its revision is V1 or V2. The guard exists so this
 * narrowing is a checked fact rather than a cast, and so a store that ever
 * contradicted that contract would say which contract it broke instead of
 * rendering a workflow with no nodes as an empty one.
 *
 * That is also why the call site in the run cockpit's markup needs no fallback:
 * it narrows the revision of an existing run, and an existing run has already
 * passed the refusal above.
 */
export function executableGraph(graph: WorkflowGraph): ExecutableWorkflowGraph {
  if (graph.format_version === 3) {
    throw new Error("a run cannot hold a revision no run can start");
  }
  return graph;
}
export type AuthProfileInput = z.infer<typeof authProfileInputSchema>;
export type AuthProfileRevision = z.infer<typeof authProfileRevisionSchema>;
export type AgentConfigurationInput = z.infer<typeof agentConfigurationInputSchema>;
export type AgentConfigurationRevision = z.infer<typeof agentConfigurationRevisionSchema>;

export interface HttpResult<T> {
  status: number;
  value: T;
}

export interface CockpitApi {
  listRuns(after?: string): Promise<RunPage>;
  listWorkflowRevisions(after?: string): Promise<WorkflowRevisionPage>;
  publish(mutation: PublishMutation): Promise<HttpResult<WorkflowRevisionDetail>>;
  publishAuthProfile(input: AuthProfileInput): Promise<HttpResult<AuthProfileRevision>>;
  publishAgentConfiguration(
    input: AgentConfigurationInput
  ): Promise<HttpResult<AgentConfigurationRevision>>;
  start(mutation: StartMutation): Promise<HttpResult<Run>>;
  answer(mutation: WaitMutation): Promise<HttpResult<Run>>;
  reconcile(mutation: ReconciliationMutation): Promise<HttpResult<Run>>;
  getRun(publicReference: string): Promise<AnyRun>;
  getWorkflowRevision(revisionHash: string): Promise<WorkflowRevisionDetail>;
  openRunEvents(publicReference: string, handlers: RunEventHandlers): RunEventSubscription;
}

export interface RunEventHandlers {
  opened(): void;
  event(rawData: string): void;
  disconnected(): void;
}

export interface RunEventSubscription {
  close(): void;
}

export interface EventSourcePort extends RunEventSubscription {
  addEventListener(type: string, listener: EventListener): void;
}

export type EventSourceFactory = (target: string) => EventSourcePort;

export class CockpitRequestError extends Error {
  constructor(
    message: string,
    readonly problem: Problem | null = null,
    readonly definitive_failure = false
  ) {
    super(message);
  }
}

export function createCockpitApi(
  fetcher: typeof fetch = globalThis.fetch,
  eventSourceFactory: EventSourceFactory = (target) => new EventSource(target)
): CockpitApi {
  return {
    listRuns: (after?: string) =>
      requestJson(
        fetcher,
        after === undefined
          ? "/atelier/api/v1/runs?limit=50"
          : `/atelier/api/v1/runs?limit=50&after=${encodeURIComponent(after)}`,
        {},
        [200],
        runPageSchema
      ),
    listWorkflowRevisions: (after?: string) =>
      requestJson(
        fetcher,
        after === undefined
          ? "/atelier/api/v1/workflow-revisions?limit=50&view=described"
          : `/atelier/api/v1/workflow-revisions?limit=50&view=described&after=${encodeURIComponent(after)}`,
        {},
        [200],
        workflowRevisionPageSchema
      ),
    publish: async (mutation) =>
      requestJsonResult(
        fetcher,
        mutation.target,
        {
          method: "POST",
          headers: { "content-type": "application/yaml" },
          body: exactBody(mutation.body_base64)
        },
        [200, 201],
        workflowRevisionDetailSchema
      ),
    publishAuthProfile: async (input) =>
      requestJsonResult(
        fetcher,
        "/atelier/api/v1/auth-profile-revisions",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(authProfileInputSchema.parse(input))
        },
        [200, 201],
        authProfileRevisionSchema
      ),
    publishAgentConfiguration: async (input) =>
      requestJsonResult(
        fetcher,
        "/atelier/api/v1/agent-configuration-revisions",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(agentConfigurationInputSchema.parse(input))
        },
        [200, 201],
        agentConfigurationRevisionSchema
      ),
    start: async (mutation) =>
      requestJsonResult(
        fetcher,
        mutation.target,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: exactBody(mutation.body_base64)
        },
        [200, 201],
        runSchema
      ),
    answer: async (mutation) => {
      const result = await requestJsonResult(
        fetcher,
        mutation.target,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: exactBody(mutation.body_base64)
        },
        [200, 202],
        runSchema
      );
      if (
        result.value.public_run_reference !== mutation.public_run_reference ||
        result.value.workflow_revision_hash !== mutation.workflow_revision_hash
      ) {
        throw new CockpitRequestError("The answer response did not match the exact durable run.");
      }
      return result;
    },
    reconcile: async (mutation) => {
      const result = await requestJsonResult(
        fetcher,
        mutation.target,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: exactBody(mutation.body_base64)
        },
        [200, 202],
        runSchema
      );
      const target = `/atelier/api/v1/runs/${result.value.public_run_reference}/reconciliations`;
      if (
        target !== mutation.target ||
        result.value.workflow_revision_hash !== mutation.workflow_revision_hash
      ) {
        throw new CockpitRequestError(
          "The reconciliation response did not match the exact durable run."
        );
      }
      return result;
    },
    getRun: (publicReference) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/runs/${encodeURIComponent(publicReference)}`,
        {},
        [200],
        anyRunSchema
      ),
    getWorkflowRevision: async (revisionHash) => {
      const revision = await requestJson(
        fetcher,
        `/atelier/api/v1/workflow-revisions/${encodeURIComponent(revisionHash)}`,
        {},
        [200],
        workflowRevisionDetailSchema
      );
      if (revision.revision_hash !== revisionHash) {
        throw new CockpitRequestError("The workflow response did not match the requested revision.");
      }
      return revision;
    },
    openRunEvents: (publicReference, handlers) => {
      if (decodePublicRunReference(publicReference) === null) {
        throw new CockpitRequestError("The run event target was not a valid public reference.");
      }
      const source = eventSourceFactory(
        `/atelier/api/v1/runs/${encodeURIComponent(publicReference)}/events`
      );
      source.addEventListener("open", () => handlers.opened());
      source.addEventListener("message", (event) => {
        if (event instanceof MessageEvent && typeof event.data === "string") {
          handlers.event(event.data);
        }
      });
      source.addEventListener("error", () => handlers.disconnected());
      return source;
    }
  };
}

export function decodeProblem(value: unknown): Problem {
  return problemSchema.parse(value);
}

export function decodeRun(value: unknown): Run {
  return runSchema.parse(value);
}

export function decodeRunEvent(value: unknown): RunEvent {
  return runEventSchema.parse(value);
}

export function decodeStreamFrame(value: unknown): StreamFrame {
  return streamFrameSchema.parse(value);
}

export function isStreamFailure(frame: StreamFrame): frame is StreamFailure {
  return frame.event === "STREAM_FAILED";
}

export function decodeWorkflowRevisionDetail(value: unknown): WorkflowRevisionDetail {
  return workflowRevisionDetailSchema.parse(value);
}

async function requestJson<T>(
  fetcher: typeof fetch,
  target: string,
  init: RequestInit,
  acceptedStatuses: readonly number[],
  schema: z.ZodType<T>
): Promise<T> {
  return (await requestJsonResult(fetcher, target, init, acceptedStatuses, schema)).value;
}

async function requestJsonResult<T>(
  fetcher: typeof fetch,
  target: string,
  init: RequestInit,
  acceptedStatuses: readonly number[],
  schema: z.ZodType<T>
): Promise<HttpResult<T>> {
  let response: Response;
  try {
    response = await fetcher(target, { ...init, headers: { accept: "application/json", ...init.headers } });
  } catch (error) {
    throw new CockpitRequestError(errorMessage(error));
  }
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new CockpitRequestError("The API response was not valid JSON.");
  }
  if (!acceptedStatuses.includes(response.status)) {
    try {
      const problem = decodeProblem(value);
      if (problem.status !== response.status) {
        throw new CockpitRequestError("The problem body disagreed with the HTTP status.");
      }
      throw new CockpitRequestError(problem.detail, problem, problem.status < 500);
    } catch (error) {
      if (error instanceof CockpitRequestError) throw error;
      throw new CockpitRequestError(`The API returned undocumented HTTP ${response.status}.`);
    }
  }
  try {
    return { status: response.status, value: schema.parse(value) };
  } catch {
    throw new CockpitRequestError("The API response did not match the durable wire contract.");
  }
}

function exactBody(bodyBase64: string): ArrayBuffer {
  const bytes = decodeCanonicalBase64(bodyBase64);
  if (bytes === null) {
    throw new CockpitRequestError("The saved exact request bytes are corrupt.");
  }
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The API request failed.";
}

function problemVariant<
  const Code extends keyof typeof problemDefinitions,
  const Title extends (typeof problemDefinitions)[Code]["title"],
  const Status extends (typeof problemDefinitions)[Code]["status"]
>(code: Code, definition: { readonly title: Title; readonly status: Status }) {
  return z
    .object({
      type: z.literal(`urn:atelier2:problem:v1:${code}` as const),
      title: z.literal(definition.title),
      status: z.literal(definition.status),
      detail: z.string()
    })
    .strict();
}

function isCanonicalStandardBase64(value: string): boolean {
  return decodeCanonicalBase64(value) !== null;
}

export function decodeCanonicalBase64(value: string): Uint8Array | null {
  if (
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)
  ) {
    return null;
  }
  try {
    const binary = atob(value);
    if (btoa(binary) !== value) {
      return null;
    }
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

export function decodePublicRunReference(reference: string): string | null {
  if (!reference.startsWith("run1.")) {
    return null;
  }
  const encoded = reference.slice("run1.".length);
  if (!/^[A-Za-z0-9_-]+$/.test(encoded)) {
    return null;
  }
  try {
    const standard = encoded.replaceAll("-", "+").replaceAll("_", "/");
    const binary = atob(standard + "=".repeat((4 - (standard.length % 4)) % 4));
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const runId = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    if (runId.length === 0 || encodePublicRunReference(runId) !== reference) {
      return null;
    }
    return runId;
  } catch {
    return null;
  }
}

export function encodePublicRunReference(runId: string): string {
  const bytes = new TextEncoder().encode(runId);
  const binary = String.fromCharCode(...bytes);
  return `run1.${btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "")}`;
}

function parseEventCursor(
  cursor: string
): { publicRunReference: string; sequence: number } | null {
  const match = /^event1\.([A-Za-z0-9_-]+)\.([1-9][0-9]*)$/.exec(cursor);
  if (match === null) {
    return null;
  }
  const encodedRun = match[1];
  const encodedSequence = match[2];
  if (encodedRun === undefined || encodedSequence === undefined) {
    return null;
  }
  const publicReference = `run1.${encodedRun}`;
  const sequence = Number(encodedSequence);
  if (decodePublicRunReference(publicReference) === null || !Number.isSafeInteger(sequence)) {
    return null;
  }
  return { publicRunReference: publicReference, sequence };
}
