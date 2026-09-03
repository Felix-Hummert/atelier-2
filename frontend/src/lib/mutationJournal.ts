import {
  decodeCanonicalBase64,
  decodePublicRunReference
} from "../api/client";
import { sha256Hex } from "./exactBytes";
import { MUTATION_JOURNAL_STORAGE_KEY } from "./storageKeys";

/**
 * How far a journaled mutation got on the wire.
 *
 * `prepared` is saved but not yet known to have reached the server; `uncertain`
 * was sent without a confirming response, so a retry replays the exact command;
 * `accepted` carries the server's own durable 202 -- the command is committed
 * server-side and only its terminal effect is still arriving. A cancel needs
 * that third word so a reload can still say "Stopping this run" honestly for an
 * accepted cancel while offering Retry/Discard for one that was never confirmed.
 */
export type MutationDelivery = "prepared" | "uncertain" | "accepted";

interface MutationBase {
  mutation_id: string;
  target: string;
  body_base64: string;
}

export interface PublishMutation extends MutationBase {
  kind: "publish";
  content_type: "application/yaml";
  revision_hash: string;
}

export interface StartMutation extends MutationBase {
  kind: "start";
  content_type: "application/json";
}

export interface StartAgentBinding {
  role: string;
  agent_configuration_revision_hash: string;
}

export async function publicationMutation(document: string): Promise<PublishMutation> {
  const bytes = new TextEncoder().encode(document);
  const revisionHash = await sha256Hex(bytes);
  return {
    mutation_id: `publish:${revisionHash}`,
    kind: "publish",
    target: "/atelier/api/v1/workflow-revisions",
    content_type: "application/yaml",
    body_base64: encodeBase64(bytes),
    revision_hash: revisionHash
  };
}

export type StartOrder =
  | { name: string; value: string }
  | { name: string; work_item: string };

export function startMutation(
  runId: string,
  workflowRevisionHash: string,
  agentBindings: readonly StartAgentBinding[],
  orders: readonly StartOrder[]
): StartMutation {
  const body = new TextEncoder().encode(
    JSON.stringify({
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: workflowRevisionHash,
      agent_bindings: agentBindings,
      orders
    })
  );
  return {
    mutation_id: `start:${runId}`,
    kind: "start",
    target: "/atelier/api/v1/runs",
    content_type: "application/json",
    body_base64: encodeBase64(body)
  };
}

export function createRunId(): string {
  return `run-${globalThis.crypto.randomUUID()}`;
}

export interface WaitMutation extends MutationBase {
  kind: "wait";
  content_type: "application/json";
  public_run_reference: string;
  workflow_revision_hash: string;
  node_id: string;
  expected_node_execution_id: string;
  actor: "operator";
  answer_base64: string;
  answer_hash: string;
}

export function waitMutationId(publicRunReference: string, nodeExecutionId: string): string {
  return `wait:${publicRunReference}:${nodeExecutionId}`;
}

export async function waitMutation(
  publicRunReference: string,
  workflowRevisionHash: string,
  nodeId: string,
  expectedNodeExecutionId: string,
  answer: string
): Promise<WaitMutation> {
  if (answer.length === 0) {
    throw new Error("wait answer must not be empty");
  }
  const answerBytes = new TextEncoder().encode(answer);
  const answerBase64 = encodeBase64(answerBytes);
  const body = new TextEncoder().encode(
    JSON.stringify({
      workflow_revision_hash: workflowRevisionHash,
      node_id: nodeId,
      expected_node_execution_id: expectedNodeExecutionId,
      actor: "operator",
      answer_base64: answerBase64
    })
  );
  return {
    mutation_id: waitMutationId(publicRunReference, expectedNodeExecutionId),
    kind: "wait",
    target: `/atelier/api/v1/runs/${publicRunReference}/answers`,
    content_type: "application/json",
    body_base64: encodeBase64(body),
    public_run_reference: publicRunReference,
    workflow_revision_hash: workflowRevisionHash,
    node_id: nodeId,
    expected_node_execution_id: expectedNodeExecutionId,
    actor: "operator",
    answer_base64: answerBase64,
    answer_hash: await sha256Hex(answerBytes)
  };
}

export function waitAnswerText(mutation: WaitMutation): string {
  const bytes = decodeCanonicalBase64(mutation.answer_base64);
  const answer = bytes === null ? null : decodeUtf8(bytes);
  if (answer === null || answer.length === 0) {
    throw new Error("saved wait answer is not readable text");
  }
  return answer;
}

/**
 * One operator's confirmed V3 run-cancel, journaled before it reaches the wire.
 *
 * The client carries only the opaque `idempotency_key` it repeats on retry;
 * #439's server mints the durable command id into its reserved namespace, so a
 * lost response replays the exact same command instead of minting a second
 * cancel. `expected_node_execution_id` is D2's fence -- the exact
 * `cancellation.target_node_execution_id` the run just served -- so a cancel
 * confirmed in one loop round can never stop another round's attempt. The
 * journal identity keys on that target, so a run that moved to a new round
 * offers a fresh cancel rather than replaying the stale one.
 */
export interface CancelMutation extends MutationBase {
  kind: "cancel";
  content_type: "application/json";
  public_run_reference: string;
  expected_node_execution_id: string;
  idempotency_key: string;
}

export function createCancelIdempotencyKey(): string {
  return `cancel-${globalThis.crypto.randomUUID()}`;
}

export function cancelMutationId(
  publicRunReference: string,
  expectedNodeExecutionId: string
): string {
  return `cancel:${publicRunReference}:${expectedNodeExecutionId}`;
}

export function cancelMutation(
  publicRunReference: string,
  expectedNodeExecutionId: string,
  idempotencyKey: string
): CancelMutation {
  if (
    decodePublicRunReference(publicRunReference) === null ||
    !digestPattern.test(expectedNodeExecutionId) ||
    idempotencyKey.length === 0
  ) {
    throw new Error("invalid cancel identity or idempotency key");
  }
  const body = new TextEncoder().encode(
    JSON.stringify({
      idempotency_key: idempotencyKey,
      expected_node_execution_id: expectedNodeExecutionId
    })
  );
  return {
    mutation_id: cancelMutationId(publicRunReference, expectedNodeExecutionId),
    kind: "cancel",
    target: `/atelier/api/v1/runs/${publicRunReference}/cancellations`,
    content_type: "application/json",
    body_base64: encodeBase64(body),
    public_run_reference: publicRunReference,
    expected_node_execution_id: expectedNodeExecutionId,
    idempotency_key: idempotencyKey
  };
}

export type MutationEnvelope =
  | PublishMutation
  | StartMutation
  | WaitMutation
  | CancelMutation;

export type JournalEntry = MutationEnvelope & { delivery: MutationDelivery };

interface RequestBoundEvidence {
  status: number;
  target: string;
  request_body_base64: string;
}

export type MutationEvidence =
  | (RequestBoundEvidence & {
      type: "publication_response";
      revision_hash: string;
      document_base64: string;
    })
  | (RequestBoundEvidence & {
      type: "start_response";
      run_id: string;
      public_run_reference: string;
      workflow_revision_hash: string;
    })
  | (RequestBoundEvidence & { type: "wait_response" })
  | (RequestBoundEvidence & { type: "cancel_response" })
  | {
      type: "wait_answered";
      public_run_reference: string;
      workflow_revision_hash: string;
      node_id: string;
      node_execution_id: string;
      answer: string;
      answer_hash: string;
    };

const digestPattern = /^[0-9a-f]{64}$/;

export class MutationJournal {
  constructor(private readonly storage: Storage) {}

  async prepare(envelope: MutationEnvelope): Promise<JournalEntry> {
    await requireEnvelope(envelope);
    const entries = await this.entries();
    const existing = entries.find((entry) => entry.mutation_id === envelope.mutation_id);
    if (existing !== undefined) {
      if (!sameEnvelope(existing, envelope)) {
        throw new Error("mutation identity already belongs to a different exact request");
      }
      return existing;
    }
    const prepared = { ...envelope, delivery: "prepared" } as JournalEntry;
    this.write([...entries, prepared]);
    return prepared;
  }

  async markUncertain(mutationId: string): Promise<JournalEntry> {
    const entries = await this.entries();
    const index = entries.findIndex((entry) => entry.mutation_id === mutationId);
    const current = entries[index];
    if (index < 0 || current === undefined) {
      throw new Error("cannot mark an unknown mutation uncertain");
    }
    const uncertain = { ...current, delivery: "uncertain" } as JournalEntry;
    entries[index] = uncertain;
    this.write(entries);
    return uncertain;
  }

  async markAccepted(mutationId: string): Promise<JournalEntry> {
    const entries = await this.entries();
    const index = entries.findIndex((entry) => entry.mutation_id === mutationId);
    const current = entries[index];
    if (index < 0 || current === undefined) {
      throw new Error("cannot mark an unknown mutation accepted");
    }
    const accepted = { ...current, delivery: "accepted" } as JournalEntry;
    entries[index] = accepted;
    this.write(entries);
    return accepted;
  }

  async get(mutationId: string): Promise<JournalEntry | null> {
    return (await this.entries()).find((entry) => entry.mutation_id === mutationId) ?? null;
  }

  async entries(): Promise<JournalEntry[]> {
    const stored = this.storage.getItem(MUTATION_JOURNAL_STORAGE_KEY);
    if (stored === null) {
      return [];
    }
    let value: unknown;
    try {
      value = JSON.parse(stored);
    } catch (error) {
      throw new Error("mutation journal is not valid JSON", { cause: error });
    }
    if (!Array.isArray(value)) {
      throw new Error("mutation journal must contain a list");
    }
    const entries = await Promise.all(value.map(requireJournalEntry));
    const identities = new Set<string>();
    for (const entry of entries) {
      if (identities.has(entry.mutation_id)) {
        throw new Error("mutation journal contains a duplicate mutation identity");
      }
      identities.add(entry.mutation_id);
    }
    return entries;
  }

  async resolve(mutationId: string, evidence: MutationEvidence): Promise<boolean> {
    const entries = await this.entries();
    const entry = entries.find((candidate) => candidate.mutation_id === mutationId);
    if (entry === undefined || !evidenceMatches(entry, evidence)) {
      return false;
    }
    this.write(entries.filter((candidate) => candidate.mutation_id !== mutationId));
    return true;
  }

  async discard(mutationId: string): Promise<boolean> {
    const entries = await this.entries();
    const remaining = entries.filter((entry) => entry.mutation_id !== mutationId);
    if (remaining.length === entries.length) {
      return false;
    }
    this.write(remaining);
    return true;
  }

  /**
   * Forgets everything this browser remembered, without reading any of it
   * first (#914).
   *
   * `entries()` rejects a poisoned journal by design -- corrupt JSON, an
   * unknown field, a duplicate identity, a bad hash are all the truth, never
   * something to tolerate -- so every other method on this class, which reads
   * before it writes, stays blocked by the same poisoned entry it would need
   * to discard. This is the one path out: it never parses, so it can never
   * throw on what it is asked to remove. Returns whether anything was stored
   * to forget, so a caller can tell an honest no-op from an actual discard.
   */
  discardPoisoned(): boolean {
    const stored = this.storage.getItem(MUTATION_JOURNAL_STORAGE_KEY);
    if (stored === null) {
      return false;
    }
    this.storage.removeItem(MUTATION_JOURNAL_STORAGE_KEY);
    return true;
  }

  private write(entries: JournalEntry[]): void {
    if (entries.length === 0) {
      this.storage.removeItem(MUTATION_JOURNAL_STORAGE_KEY);
    } else {
      this.storage.setItem(MUTATION_JOURNAL_STORAGE_KEY, JSON.stringify(entries));
    }
  }
}

async function requireJournalEntry(value: unknown): Promise<JournalEntry> {
  if (
    !isRecord(value) ||
    (value.delivery !== "prepared" &&
      value.delivery !== "uncertain" &&
      value.delivery !== "accepted")
  ) {
    throw new Error("mutation journal entry has an unknown delivery state");
  }
  const envelope = { ...value };
  delete envelope.delivery;
  const typedEnvelope = envelope as unknown as MutationEnvelope;
  await requireEnvelope(typedEnvelope);
  const expectedKeys = [...envelopeKeys(typedEnvelope), "delivery"].sort();
  requireExactKeys(value, expectedKeys);
  return value as unknown as JournalEntry;
}

async function requireEnvelope(envelope: MutationEnvelope): Promise<void> {
  if (!isRecord(envelope) || typeof envelope.kind !== "string") {
    throw new Error("invalid mutation journal envelope");
  }
  switch (envelope.kind) {
    case "publish":
      await requirePublish(envelope as PublishMutation);
      return;
    case "start":
      requireStart(envelope as StartMutation);
      return;
    case "wait":
      await requireWait(envelope as WaitMutation);
      return;
    case "cancel":
      requireCancel(envelope as CancelMutation);
      return;
    default:
      throw new Error("invalid mutation journal envelope");
  }
}

async function requirePublish(envelope: PublishMutation): Promise<void> {
  requireExactKeys(envelope, envelopeKeys(envelope));
  if (
    envelope.target !== "/atelier/api/v1/workflow-revisions" ||
    envelope.content_type !== "application/yaml" ||
    !digestPattern.test(envelope.revision_hash) ||
    envelope.mutation_id !== `publish:${envelope.revision_hash}`
  ) {
    throw new Error("invalid publish mutation envelope");
  }
  const document = decodeCanonicalBase64(envelope.body_base64);
  if (document === null || (await sha256Hex(document)) !== envelope.revision_hash) {
    throw new Error("publish revision identity differs from its exact document bytes");
  }
}

function requireStart(envelope: StartMutation): void {
  requireExactKeys(envelope, envelopeKeys(envelope));
  const body = requireStartBody(envelope.body_base64);
  if (
    envelope.target !== "/atelier/api/v1/runs" ||
    envelope.content_type !== "application/json" ||
    envelope.mutation_id !== `start:${body.run_id}`
  ) {
    throw new Error("invalid start mutation envelope");
  }
}

function requireStartBody(bodyBase64: string): {
  workflow_format_version: 3;
  run_id: string;
  workflow_revision_hash: string;
  agent_bindings: StartAgentBinding[];
  orders: StartOrder[];
} {
  const body = requireJsonBody(bodyBase64);
  requireExactKeys(body, [
    "workflow_format_version",
    "run_id",
    "workflow_revision_hash",
    "agent_bindings",
    "orders"
  ]);
  if (
    body.workflow_format_version !== 3 ||
    typeof body.run_id !== "string" ||
    body.run_id.length === 0 ||
    typeof body.workflow_revision_hash !== "string" ||
    !digestPattern.test(body.workflow_revision_hash)
  ) throw new Error("invalid start mutation body");
  return {
    workflow_format_version: 3,
    run_id: body.run_id,
    workflow_revision_hash: body.workflow_revision_hash,
    agent_bindings: requireStartAgentBindings(body.agent_bindings),
    orders: requireStartOrders(body.orders)
  };
}

function requireStartAgentBindings(value: unknown): StartAgentBinding[] {
  if (!Array.isArray(value)) throw new Error("invalid start mutation bindings");
  const roles = new Set<string>();
  const bindings: StartAgentBinding[] = [];
  for (const entry of value) {
    if (!isRecord(entry)) throw new Error("invalid start mutation binding");
    requireExactKeys(entry, ["role", "agent_configuration_revision_hash"]);
    if (
      typeof entry.role !== "string" ||
      entry.role.length === 0 ||
      roles.has(entry.role) ||
      typeof entry.agent_configuration_revision_hash !== "string" ||
      !digestPattern.test(entry.agent_configuration_revision_hash)
    ) {
      throw new Error("invalid start mutation binding");
    }
    roles.add(entry.role);
    bindings.push({
      role: entry.role,
      agent_configuration_revision_hash: entry.agent_configuration_revision_hash
    });
  }
  return bindings;
}

function requireStartOrders(value: unknown): StartOrder[] {
  if (!Array.isArray(value)) throw new Error("invalid start mutation orders");
  const names = new Set<string>();
  const orders: StartOrder[] = [];
  for (const entry of value) {
    if (!isRecord(entry)) throw new Error("invalid start mutation order");
    const isWorkItem = "work_item" in entry;
    requireExactKeys(entry, isWorkItem ? ["name", "work_item"] : ["name", "value"]);
    if (
      typeof entry.name !== "string" ||
      entry.name.length === 0 ||
      names.has(entry.name) ||
      (isWorkItem
        ? typeof entry.work_item !== "string" || entry.work_item.length === 0
        : typeof entry.value !== "string" || entry.value.length === 0)
    ) {
      throw new Error("invalid start mutation order");
    }
    names.add(entry.name);
    orders.push(isWorkItem ? { name: entry.name, work_item: entry.work_item as string } : { name: entry.name, value: entry.value as string });
  }
  return orders;
}

async function requireWait(envelope: WaitMutation): Promise<void> {
  requireExactKeys(envelope, envelopeKeys(envelope));
  const route = /^\/atelier\/api\/v1\/runs\/(run1\.[A-Za-z0-9_-]+)\/answers$/.exec(
    envelope.target
  );
  const publicReference = route?.[1];
  const body = requireJsonBody(envelope.body_base64);
  requireExactKeys(body, [
    "workflow_revision_hash",
    "node_id",
    "expected_node_execution_id",
    "actor",
    "answer_base64"
  ]);
  const answerBytes =
    typeof body.answer_base64 === "string"
      ? decodeCanonicalBase64(body.answer_base64)
      : null;
  const answer = answerBytes === null ? null : decodeUtf8(answerBytes);
  if (
    envelope.content_type !== "application/json" ||
    publicReference === undefined ||
    decodePublicRunReference(publicReference) === null ||
    envelope.public_run_reference !== publicReference ||
    typeof body.workflow_revision_hash !== "string" ||
    !digestPattern.test(body.workflow_revision_hash) ||
    envelope.workflow_revision_hash !== body.workflow_revision_hash ||
    typeof body.node_id !== "string" ||
    body.node_id.length === 0 ||
    envelope.node_id !== body.node_id ||
    typeof body.expected_node_execution_id !== "string" ||
    !digestPattern.test(body.expected_node_execution_id) ||
    envelope.expected_node_execution_id !== body.expected_node_execution_id ||
    body.actor !== "operator" ||
    envelope.actor !== body.actor ||
    envelope.answer_base64 !== body.answer_base64 ||
    answer === null ||
    answer.length === 0 ||
    envelope.mutation_id !== waitMutationId(publicReference, body.expected_node_execution_id)
  ) {
    throw new Error("invalid wait mutation envelope");
  }
  if (answerBytes === null || (await sha256Hex(answerBytes)) !== envelope.answer_hash) {
    throw new Error("wait answer identity differs from its exact bytes");
  }
}

function requireCancel(envelope: CancelMutation): void {
  requireExactKeys(envelope, envelopeKeys(envelope));
  const route = /^\/atelier\/api\/v1\/runs\/(run1\.[A-Za-z0-9_-]+)\/cancellations$/.exec(
    envelope.target
  );
  const publicReference = route?.[1];
  const body = requireJsonBody(envelope.body_base64);
  requireExactKeys(body, ["idempotency_key", "expected_node_execution_id"]);
  if (
    envelope.content_type !== "application/json" ||
    publicReference === undefined ||
    decodePublicRunReference(publicReference) === null ||
    envelope.public_run_reference !== publicReference ||
    typeof body.idempotency_key !== "string" ||
    body.idempotency_key.length === 0 ||
    envelope.idempotency_key !== body.idempotency_key ||
    typeof body.expected_node_execution_id !== "string" ||
    !digestPattern.test(body.expected_node_execution_id) ||
    envelope.expected_node_execution_id !== body.expected_node_execution_id ||
    envelope.mutation_id !== cancelMutationId(publicReference, body.expected_node_execution_id)
  ) {
    throw new Error("invalid cancel mutation envelope");
  }
}

function evidenceMatches(
  entry: JournalEntry,
  evidence: MutationEvidence
): boolean {
  switch (entry.kind) {
    case "publish":
      return (
        evidence.type === "publication_response" &&
        (evidence.status === 200 || evidence.status === 201) &&
        requestEvidenceMatches(entry, evidence) &&
        evidence.revision_hash === entry.revision_hash &&
        evidence.document_base64 === entry.body_base64
      );
    case "start": {
      if (
        evidence.type !== "start_response" ||
        (evidence.status !== 200 && evidence.status !== 201) ||
        !requestEvidenceMatches(entry, evidence)
      ) {
        return false;
      }
      const body = requireJsonBody(entry.body_base64);
      return (
        evidence.run_id === body.run_id &&
        evidence.workflow_revision_hash === body.workflow_revision_hash &&
        decodePublicRunReference(evidence.public_run_reference) === evidence.run_id
      );
    }
    case "wait":
      if (evidence.type === "wait_response") {
        return evidence.status === 200 && requestEvidenceMatches(entry, evidence);
      }
      return evidence.type === "wait_answered" && waitEvidenceMatches(entry, evidence);
    case "cancel":
      return (
        evidence.type === "cancel_response" &&
        evidence.status === 200 &&
        requestEvidenceMatches(entry, evidence)
      );
  }
}

function requestEvidenceMatches(
  entry: JournalEntry,
  evidence: RequestBoundEvidence
): boolean {
  return (
    Number.isSafeInteger(evidence.status) &&
    evidence.status >= 200 &&
    evidence.status <= 599 &&
    evidence.target === entry.target &&
    evidence.request_body_base64 === entry.body_base64
  );
}

function waitEvidenceMatches(
  entry: WaitMutation,
  evidence: Extract<MutationEvidence, { type: "wait_answered" }>
): boolean {
  const body = requireJsonBody(entry.body_base64);
  const answerBytes = decodeCanonicalBase64(String(body.answer_base64));
  const answer = answerBytes === null ? null : decodeUtf8(answerBytes);
  const publicReference = publicReferenceFromTarget(entry.target, "answers");
  return (
    evidence.public_run_reference === publicReference &&
    evidence.workflow_revision_hash === body.workflow_revision_hash &&
    evidence.node_id === body.node_id &&
    evidence.node_execution_id === body.expected_node_execution_id &&
    evidence.answer === answer &&
    evidence.answer_hash === entry.answer_hash
  );
}

function requireJsonBody(bodyBase64: string): Record<string, unknown> {
  const bytes = decodeCanonicalBase64(bodyBase64);
  const decoded = bytes === null ? null : decodeUtf8(bytes);
  if (decoded === null) {
    throw new Error("mutation request body is not canonical base64 UTF-8");
  }
  let value: unknown;
  try {
    value = JSON.parse(decoded);
  } catch (error) {
    throw new Error("mutation request body is not valid JSON", { cause: error });
  }
  if (!isRecord(value)) {
    throw new Error("mutation JSON body must be an object");
  }
  return value;
}

function decodeUtf8(bytes: Uint8Array): string | null {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

function publicReferenceFromTarget(target: string, operation: string): string {
  const suffix = `/${operation}`;
  return target.slice("/atelier/api/v1/runs/".length, -suffix.length);
}

function envelopeKeys(envelope: MutationEnvelope): string[] {
  const common = ["mutation_id", "kind", "target", "content_type", "body_base64"];
  switch (envelope.kind) {
    case "publish":
      return [...common, "revision_hash"];
    case "start":
      return common;
    case "wait":
      return [
        ...common,
        "public_run_reference",
        "workflow_revision_hash",
        "node_id",
        "expected_node_execution_id",
        "actor",
        "answer_base64",
        "answer_hash"
      ];
    case "cancel":
      return [
        ...common,
        "public_run_reference",
        "expected_node_execution_id",
        "idempotency_key"
      ];
  }
}

function requireExactKeys(value: object, expected: string[]): void {
  if (Object.keys(value).sort().join(",") !== [...expected].sort().join(",")) {
    throw new Error("mutation journal entry has unknown fields or missing fields");
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sameEnvelope(left: MutationEnvelope, right: MutationEnvelope): boolean {
  if (left.kind !== right.kind) {
    return false;
  }
  const keys = envelopeKeys(left);
  return keys.every(
    (key) =>
      (left as unknown as Record<string, unknown>)[key] ===
      (right as unknown as Record<string, unknown>)[key]
  );
}

function encodeBase64(bytes: Uint8Array): string {
  const chunkSize = 0x8000;
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}
