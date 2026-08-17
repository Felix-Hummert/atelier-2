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
 * A published V3 revision says what it is and whether this build runs it.
 *
 * `node_previews` is an excerpt — id, kind, role, instruction start, and the
 * authored `depends_on` edges — not the authored node. The browser must not
 * parse `document_base64` to learn the same facts. `orders` is the same class
 * of answer for the material a start must supply: name plus the schema the
 * author pinned, never the schema bytes.
 */
const workflowNodePreviewSchema = z
  .object({
    id: z.string().min(1),
    kind: z.enum(["agent", "deterministic", "wait", "subworkflow", "action"]),
    role: z.string().min(1).max(1_024).nullable(),
    instruction_start: z.string().min(1).max(120).nullable(),
    depends_on: z.array(z.string().min(1))
  })
  .strict();

export { workflowNodePreviewSchema };

const workflowDeclaredOrderSchema = z
  .object({
    name: z.string().min(1),
    schema_ref: z.string().min(1),
    schema_revision: z.string().min(1)
  })
  .strict();

export { workflowDeclaredOrderSchema };

const workflowGraphV3Schema = z
  .object({
    format_version: z.literal(3),
    executable: z.boolean(),
    not_executable_reason: z.string().nullable(),
    node_count: z.number().int().positive(),
    agent_roles: z.array(z.string().min(1)).max(100),
    orders: z.array(workflowDeclaredOrderSchema),
    node_previews: z.array(workflowNodePreviewSchema),
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

/**
 * What `GET /workflow-revisions/by-name/{name}` answers: which revision that
 * catalog name holds. The described listing does not carry lineage recency, so
 * the picker reads this existing resource for the head instead of inventing an
 * order from the hash-sorted page.
 */
export const catalogNameResolutionSchema = z
  .object({
    display_name: z.string().min(1).max(128),
    lineage_id: sha256,
    revision_hash: sha256,
    revision_number: positiveSafeInteger
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

/**
 * The listing of published agent configurations, in the item form publication
 * already answers with. Held to the frozen document by servedVocabulary.
 */
export const agentConfigurationRevisionPageSchema = z
  .object({
    items: z.array(agentConfigurationRevisionSchema),
    next_after_revision_hash: sha256.nullable()
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
 * by id and carries no `current_node` object and no `agent_attempts`. It carries
 * no `waiting` block either, although it does reach WAITING_INPUT: what a V3
 * Wait node asks and which schema judges the answer belong to the document, and
 * the rail is what marks the node owing a person a move. Widening `runSchema`
 * would push a "this format has no such thing" guard onto every reader of those
 * fields; keeping it separate leaves the V2 cockpit exactly as it was and lets
 * the V3 view render what actually exists.
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
    state: z.enum(["STARTED", "WAITING_INPUT", "COMPLETED"]),
    current_node_id: z.string().min(1),
    node_rail: z.array(nodeRailEntrySchema).min(1),
    terminal_hash: sha256.nullable(),
    latest_event_cursor: eventCursor.nullable()
  })
  .strict();

/**
 * One node of a run, as `GET /runs/{ref}/nodes/{node_id}` answers it.
 *
 * Four answers, each allowed to be absent, because the absence is the answer: a
 * node that has not run yet was asked nothing this reader can prove, wrote
 * nothing and has no receipt, and a refusal exists only where something really
 * refuses. The panel renders exactly that and invents no placeholder.
 *
 * Usage and duration are not here because no receipt holds them. The panel says
 * so rather than leaving a reader to assume the fields are coming.
 */
const nodeProvenanceSchema = z
  .object({
    role: z.string().min(1),
    provider_id: z.string().min(1),
    model: z.string().min(1),
    executor_revision: z.string().min(1),
    executor_operational_identity: z.string().min(1),
    auth_mode: z.string().min(1),
    profile_id: z.string().min(1),
    agent_configuration_revision_hash: sha256,
    request_hash: sha256,
    receipt_hash: sha256
  })
  .strict();

const nodeAnswerSchema = z
  .object({ value_base64: z.string(), value_hash: sha256 })
  .strict();

export const nodeDetailSchema = z
  .object({
    run_id: z.string().min(1),
    public_run_reference: publicRunReference,
    node_id: z.string().min(1),
    state: z.enum(NODE_STATES),
    job_base64: z.string().nullable(),
    job_hash: sha256.nullable(),
    answer: nodeAnswerSchema.nullable(),
    provenance: nodeProvenanceSchema.nullable(),
    refusal: z.string().nullable()
  })
  .strict();

export type NodeDetail = z.infer<typeof nodeDetailSchema>;

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

export const runPageSchema = z
  .object({ items: z.array(anyRunSchema), next_after: publicRunReference.nullable() })
  .strict();
/**
 * The listing decodes every run the server can answer with, including V3.
 *
 * It read only V1 and V2 while the detail page already read V3, so one V3 run in
 * the store took down every level that lists runs -- the studio and the project
 * both answered "Request failed — wire contract" for a run they only had to name
 * and link. A listing is the wrong place to be strict about a format it merely
 * shows: what a row renders is the identity and the state, which every format
 * carries.
 */

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

/**
 * A format-3 event, in the shape the service answers with.
 *
 * The forms are the served wire's own (#249), read here rather than invented a
 * third time: the API answers them, the command reads them (#253), and this is
 * the surface the operator actually watches. A version-3 line writes its agent
 * events through the same attempt store as a version-2 one and its pauses
 * through the same wait path, so the attempt and the rail travel the same way.
 * Its answer is base64 rather than the V2 shape's decimal text, because a V3
 * wait admits whatever its declared schema admits. What is absent is absent on
 * purpose: no format-3 run persists an Action or Subworkflow event today.
 */
const v3EventBase = {
  workflow_format_version: z.literal(3),
  ...eventBase,
  node_rail: z.array(nodeRailEntrySchema).min(1)
};

const runEventV3Schema = z
  .discriminatedUnion("event", [
    z.object({ ...v3EventBase, ...v2AttemptEvent, event: z.literal("AGENT_COMPLETED"), output_base64: standardBase64, output_hash: sha256 }).strict(),
    z.object({ ...v3EventBase, ...v2AttemptEvent, event: z.literal("AGENT_FAILED"), failure_code: z.literal("PROCESS_EXITED_UNSUCCESSFULLY") }).strict(),
    z.object({ ...v3EventBase, ...v2CancellationEvent, event: z.literal("AGENT_CANCEL_REQUESTED") }).strict(),
    z.object({ ...v3EventBase, ...v2CancellationEvent, event: z.literal("AGENT_CANCELLED"), disposition: v2Disposition, replacement_attempt_id: sha256.nullable() }).strict(),
    z.object({ ...v3EventBase, ...v2CancellationEvent, event: z.literal("AGENT_INTERRUPTED"), disposition: v2Disposition, replacement_attempt_id: sha256.nullable() }).strict(),
    z.object({ ...v3EventBase, event: z.literal("WAITING_INPUT") }).strict(),
    z.object({ ...v3EventBase, event: z.literal("WAIT_ANSWERED"), answer_base64: standardBase64, answer_hash: sha256 }).strict()
  ])
  .superRefine(validateEventCursor);

export const runEventSchema = z.union([
  runEventV3Schema,
  runEventV2Schema,
  runEventV1Schema
]);

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
  "invalid-agent-attempt-id": { status: 400, title: "Invalid agent attempt id" },
  "agent-attempt-not-found": { status: 404, title: "Agent attempt not found" },
  "agent-attempt-not-current": { status: 409, title: "Agent attempt is not current" },
  "agent-attempt-cancellation-stale": { status: 409, title: "Agent attempt cancellation is stale" },
  "agent-attempt-terminal": { status: 409, title: "Agent attempt is terminal" },
  "cancellation-command-conflict": { status: 409, title: "Cancellation command conflict" },
  "replacement-not-allowed": { status: 409, title: "Replacement is not allowed" },
  "invalid-public-run-reference": { status: 400, title: "Invalid public run reference" },
  "invalid-event-cursor": { status: 400, title: "Invalid event cursor" },
  "invalid-revision-hash": { status: 400, title: "Invalid revision hash" },
  "event-cursor-run-mismatch": { status: 409, title: "Event cursor belongs to another run" },
  "event-cursor-ahead": { status: 409, title: "Event cursor is ahead of durable history" },
  "invalid-request": { status: 422, title: "Invalid request" },
  "invalid-base64": { status: 422, title: "Invalid base64" },
  "invalid-workflow-document": { status: 422, title: "Invalid workflow document" },
  "schema-document-too-large": { status: 422, title: "Invalid schema document" },
  "schema-document-not-utf8": { status: 422, title: "Invalid schema document" },
  "schema-document-carries-byte-order-mark": { status: 422, title: "Invalid schema document" },
  "schema-document-not-json": { status: 422, title: "Invalid schema document" },
  "schema-non-canonical-number": { status: 422, title: "Invalid schema document" },
  "schema-duplicate-object-key": { status: 422, title: "Invalid schema document" },
  "schema-document-too-deep": { status: 422, title: "Invalid schema document" },
  "schema-too-many-values": { status: 422, title: "Invalid schema document" },
  "schema-forbidden-keyword": { status: 422, title: "Invalid schema document" },
  "schema-nonlocal-reference": { status: 422, title: "Invalid schema document" },
  "schema-unresolvable-reference": { status: 422, title: "Invalid schema document" },
  "schema-non-terminating-reference-cycle": { status: 422, title: "Invalid schema document" },
  "schema-unsupported-dialect": { status: 422, title: "Invalid schema document" },
  "schema-not-a-schema": { status: 422, title: "Invalid schema document" },
  "schema-revision-collision": { status: 409, title: "Schema revision collision" },
  "unsupported-media-type": { status: 415, title: "Unsupported media type" },
  "not-acceptable": { status: 406, title: "Not acceptable" },
  "catalog-revision-unpublished": { status: 409, title: "Catalog revision is unpublished" },
  "catalog-name-held": { status: 409, title: "Catalog name is held" },
  "catalog-revision-owned": { status: 409, title: "Catalog revision is owned" },
  "catalog-lineage-missing": { status: 404, title: "Catalog lineage not found" },
  "catalog-name-not-found": { status: 404, title: "Catalog name not found" },
  "catalog-lineage-retired": { status: 410, title: "Catalog lineage retired" },
  "catalog-revision-not-a-member": { status: 409, title: "Catalog revision is not a member" },
  "invalid-catalog-position": { status: 400, title: "Invalid catalog position" },
  "workflow-revision-not-found": { status: 404, title: "Workflow revision not found" },
  "run-not-found": { status: 404, title: "Run not found" },
  "node-not-found": { status: 404, title: "Node not found" },
  "revision-collision": { status: 409, title: "Workflow revision collision" },
  "workflow-format-not-executable": { status: 409, title: "Workflow format is not executable" },
  "run-input-refused": { status: 422, title: "Run input refused" },
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
  problemVariant("invalid-agent-attempt-id", problemDefinitions["invalid-agent-attempt-id"]),
  problemVariant("agent-attempt-not-found", problemDefinitions["agent-attempt-not-found"]),
  problemVariant("agent-attempt-not-current", problemDefinitions["agent-attempt-not-current"]),
  problemVariant("agent-attempt-cancellation-stale", problemDefinitions["agent-attempt-cancellation-stale"]),
  problemVariant("agent-attempt-terminal", problemDefinitions["agent-attempt-terminal"]),
  problemVariant("cancellation-command-conflict", problemDefinitions["cancellation-command-conflict"]),
  problemVariant("replacement-not-allowed", problemDefinitions["replacement-not-allowed"]),
  problemVariant("invalid-public-run-reference", problemDefinitions["invalid-public-run-reference"]),
  problemVariant("invalid-event-cursor", problemDefinitions["invalid-event-cursor"]),
  problemVariant("invalid-revision-hash", problemDefinitions["invalid-revision-hash"]),
  problemVariant("event-cursor-run-mismatch", problemDefinitions["event-cursor-run-mismatch"]),
  problemVariant("event-cursor-ahead", problemDefinitions["event-cursor-ahead"]),
  problemVariant("invalid-request", problemDefinitions["invalid-request"]),
  problemVariant("invalid-base64", problemDefinitions["invalid-base64"]),
  problemVariant("invalid-workflow-document", problemDefinitions["invalid-workflow-document"]),
  problemVariant("schema-document-too-large", problemDefinitions["schema-document-too-large"]),
  problemVariant("schema-document-not-utf8", problemDefinitions["schema-document-not-utf8"]),
  problemVariant("schema-document-carries-byte-order-mark", problemDefinitions["schema-document-carries-byte-order-mark"]),
  problemVariant("schema-document-not-json", problemDefinitions["schema-document-not-json"]),
  problemVariant("schema-non-canonical-number", problemDefinitions["schema-non-canonical-number"]),
  problemVariant("schema-duplicate-object-key", problemDefinitions["schema-duplicate-object-key"]),
  problemVariant("schema-document-too-deep", problemDefinitions["schema-document-too-deep"]),
  problemVariant("schema-too-many-values", problemDefinitions["schema-too-many-values"]),
  problemVariant("schema-forbidden-keyword", problemDefinitions["schema-forbidden-keyword"]),
  problemVariant("schema-nonlocal-reference", problemDefinitions["schema-nonlocal-reference"]),
  problemVariant("schema-unresolvable-reference", problemDefinitions["schema-unresolvable-reference"]),
  problemVariant("schema-non-terminating-reference-cycle", problemDefinitions["schema-non-terminating-reference-cycle"]),
  problemVariant("schema-unsupported-dialect", problemDefinitions["schema-unsupported-dialect"]),
  problemVariant("schema-not-a-schema", problemDefinitions["schema-not-a-schema"]),
  problemVariant("schema-revision-collision", problemDefinitions["schema-revision-collision"]),
  problemVariant("unsupported-media-type", problemDefinitions["unsupported-media-type"]),
  problemVariant("not-acceptable", problemDefinitions["not-acceptable"]),
  problemVariant("catalog-revision-unpublished", problemDefinitions["catalog-revision-unpublished"]),
  problemVariant("catalog-name-held", problemDefinitions["catalog-name-held"]),
  problemVariant("catalog-revision-owned", problemDefinitions["catalog-revision-owned"]),
  problemVariant("catalog-lineage-missing", problemDefinitions["catalog-lineage-missing"]),
  problemVariant("catalog-name-not-found", problemDefinitions["catalog-name-not-found"]),
  problemVariant("catalog-lineage-retired", problemDefinitions["catalog-lineage-retired"]),
  problemVariant("catalog-revision-not-a-member", problemDefinitions["catalog-revision-not-a-member"]),
  problemVariant("invalid-catalog-position", problemDefinitions["invalid-catalog-position"]),
  problemVariant("workflow-revision-not-found", problemDefinitions["workflow-revision-not-found"]),
  problemVariant("run-not-found", problemDefinitions["run-not-found"]),
  problemVariant("node-not-found", problemDefinitions["node-not-found"]),
  problemVariant("revision-collision", problemDefinitions["revision-collision"]),
  problemVariant("workflow-format-not-executable", problemDefinitions["workflow-format-not-executable"]),
  problemVariant("run-input-refused", problemDefinitions["run-input-refused"]),
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
export type CatalogNameResolution = z.infer<typeof catalogNameResolutionSchema>;

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
export type AgentConfigurationRevisionPage = z.infer<
  typeof agentConfigurationRevisionPageSchema
>;

export interface HttpResult<T> {
  status: number;
  value: T;
}

export interface CockpitApi {
  listRuns(after?: string): Promise<RunPage>;
  listWorkflowRevisions(after?: string): Promise<WorkflowRevisionPage>;
  listAgentConfigurationRevisions(after?: string): Promise<AgentConfigurationRevisionPage>;
  getRevisionByName(name: string): Promise<CatalogNameResolution>;
  publish(mutation: PublishMutation): Promise<HttpResult<WorkflowRevisionDetail>>;
  publishAuthProfile(input: AuthProfileInput): Promise<HttpResult<AuthProfileRevision>>;
  publishAgentConfiguration(
    input: AgentConfigurationInput
  ): Promise<HttpResult<AgentConfigurationRevision>>;
  start(mutation: StartMutation): Promise<HttpResult<AnyRun>>;
  answer(mutation: WaitMutation): Promise<HttpResult<Run>>;
  reconcile(mutation: ReconciliationMutation): Promise<HttpResult<Run>>;
  getRun(publicReference: string): Promise<AnyRun>;
  getNodeDetail(publicReference: string, nodeId: string): Promise<NodeDetail>;
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
    listAgentConfigurationRevisions: (after?: string) =>
      requestJson(
        fetcher,
        after === undefined
          ? "/atelier/api/v1/agent-configuration-revisions?limit=50"
          : `/atelier/api/v1/agent-configuration-revisions?limit=50&after_revision_hash=${encodeURIComponent(after)}`,
        {},
        [200],
        agentConfigurationRevisionPageSchema
      ),
    getRevisionByName: (name: string) =>
      requestJson(
        fetcher,
        `/atelier/api/v1/workflow-revisions/by-name/${encodeURIComponent(name)}`,
        {},
        [200],
        catalogNameResolutionSchema
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
        anyRunSchema
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
    getNodeDetail: async (publicReference, nodeId) => {
      const detail = await requestJson(
        fetcher,
        `/atelier/api/v1/runs/${encodeURIComponent(publicReference)}` +
          `/nodes/${encodeURIComponent(nodeId)}`,
        {},
        [200],
        nodeDetailSchema
      );
      if (detail.node_id !== nodeId) {
        throw new CockpitRequestError("The node response named another node.");
      }
      return detail;
    },
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
  const fields = {
    type: z.literal(`urn:atelier2:problem:v1:${code}` as const),
    title: z.literal(definition.title),
    status: z.literal(definition.status),
    detail: z.string()
  };
  if (code === "invalid-request") {
    return z
      .object({
        ...fields,
        invalid_fields: z
          .array(z.object({ path: z.string().min(1), reason: z.string().min(1) }).strict())
          .optional()
      })
      .strict();
  }
  return z.object(fields).strict();
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
