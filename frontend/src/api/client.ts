import { z } from "zod";

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

const agentNodeSchema = z
  .object({
    type: z.literal("agent"),
    node_id: z.string().min(1),
    job: z.string().min(1),
    output: z.string().min(1),
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
  agentNodeSchema,
  actionNodeSchema,
  waitNodeSchema,
  subworkflowNodeSchema
]);

const workflowGraphSchema = z
  .object({
    format_version: z.literal(1),
    start_node_id: z.string().min(1),
    nodes: z.array(nodeSchema)
  })
  .strict();

const workflowRevisionSummarySchema = z
  .object({ revision_hash: sha256 })
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

export const runSchema = z
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
  .superRefine((run, context) => {
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
  });

export const runPageSchema = z
  .object({ items: z.array(runSchema), next_after: publicRunReference.nullable() })
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

export const runEventSchema = z
  .discriminatedUnion("event", [
    z.object({ ...eventBase, event: z.literal("AGENT_COMPLETED"), output: z.string(), payload_hash: sha256 }).strict(),
    z.object({ ...eventBase, event: z.literal("ACTION_RECONCILIATION_REQUIRED"), request_base64: standardBase64, request_hash: sha256 }).strict(),
    z.object({ ...eventBase, event: z.literal("ACTION_RECONCILIATION_RESOLVED"), receipt: receiptSchema }).strict(),
    z.object({ ...eventBase, event: z.literal("ACTION_COMPLETED"), receipt: receiptSchema }).strict(),
    z.object({ ...eventBase, event: z.literal("WAITING_INPUT"), answer_type: z.literal("integer") }).strict(),
    z.object({ ...eventBase, event: z.literal("WAIT_ANSWERED"), answer: z.string().regex(/^(?:0|-?[1-9][0-9]*)$/), answer_hash: sha256 }).strict(),
    z.object({ ...eventBase, event: z.literal("SUBWORKFLOW_COMPLETED"), result: safeInteger, result_hash: sha256 }).strict()
  ])
  .superRefine((event, context) => {
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
  });

export const problemDefinitions = {
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

export type Problem = z.infer<typeof problemSchema>;
export type Run = z.infer<typeof runSchema>;
export type RunEvent = z.infer<typeof runEventSchema>;
export type WorkflowRevisionDetail = z.infer<typeof workflowRevisionDetailSchema>;

export function decodeProblem(value: unknown): Problem {
  return problemSchema.parse(value);
}

export function decodeRun(value: unknown): Run {
  return runSchema.parse(value);
}

export function decodeRunEvent(value: unknown): RunEvent {
  return runEventSchema.parse(value);
}

export function decodeWorkflowRevisionDetail(value: unknown): WorkflowRevisionDetail {
  return workflowRevisionDetailSchema.parse(value);
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
