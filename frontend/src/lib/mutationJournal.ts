import {
  decodeCanonicalBase64,
  decodePublicRunReference
} from "../api/client";
import { sha256Hex } from "./exactBytes";

export type MutationDelivery = "prepared" | "uncertain";

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

export function startMutation(runId: string, workflowRevisionHash: string): StartMutation {
  const body = new TextEncoder().encode(
    JSON.stringify({ run_id: runId, workflow_revision_hash: workflowRevisionHash })
  );
  return {
    mutation_id: `start:${runId}`,
    kind: "start",
    target: "/atelier/api/v1/runs",
    content_type: "application/json",
    body_base64: encodeBase64(body)
  };
}

export function startMutationV2(
  runId: string,
  workflowRevisionHash: string,
  agentBindings: readonly StartAgentBinding[]
): StartMutation {
  const body = new TextEncoder().encode(
    JSON.stringify({
      workflow_format_version: 2,
      run_id: runId,
      workflow_revision_hash: workflowRevisionHash,
      agent_bindings: agentBindings
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

export interface StartOrder {
  name: string;
  value: string;
}

export function startMutationV3(
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

export function requestedStartAgentBindings(mutation: Pick<StartMutation, "body_base64">): readonly StartAgentBinding[] | null {
  const body = requireStartBody(mutation.body_base64);
  return "agent_bindings" in body ? body.agent_bindings : null;
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
  answer_base64: string;
  answer_hash: string;
}

export function waitMutationId(publicRunReference: string, nodeId: string): string {
  return `wait:${publicRunReference}:${nodeId}`;
}

export async function waitMutation(
  publicRunReference: string,
  workflowRevisionHash: string,
  nodeId: string,
  answer: string
): Promise<WaitMutation> {
  if (!canonicalIntegerPattern.test(answer)) {
    throw new Error("wait answer must be one canonical integer");
  }
  return waitAnswerMutation(publicRunReference, workflowRevisionHash, nodeId, answer);
}

export async function v3WaitMutation(
  publicRunReference: string,
  workflowRevisionHash: string,
  nodeId: string,
  answer: string
): Promise<WaitMutation> {
  if (answer.length === 0) {
    throw new Error("wait answer must not be empty");
  }
  return waitAnswerMutation(publicRunReference, workflowRevisionHash, nodeId, answer);
}

async function waitAnswerMutation(
  publicRunReference: string,
  workflowRevisionHash: string,
  nodeId: string,
  answer: string
): Promise<WaitMutation> {
  const answerBytes = new TextEncoder().encode(answer);
  const answerBase64 = encodeBase64(answerBytes);
  const body = new TextEncoder().encode(
    JSON.stringify({
      revision_hash: workflowRevisionHash,
      node_id: nodeId,
      answer_base64: answerBase64
    })
  );
  return {
    mutation_id: waitMutationId(publicRunReference, nodeId),
    kind: "wait",
    target: `/atelier/api/v1/runs/${publicRunReference}/answers`,
    content_type: "application/json",
    body_base64: encodeBase64(body),
    public_run_reference: publicRunReference,
    workflow_revision_hash: workflowRevisionHash,
    node_id: nodeId,
    answer_base64: answerBase64,
    answer_hash: await sha256Hex(answerBytes)
  };
}

export function waitAnswer(mutation: WaitMutation): string {
  const answer = waitAnswerText(mutation);
  if (!canonicalIntegerPattern.test(answer)) {
    throw new Error("saved wait answer is not one canonical integer");
  }
  return answer;
}

export function waitAnswerText(mutation: WaitMutation): string {
  const bytes = decodeCanonicalBase64(mutation.answer_base64);
  const answer = bytes === null ? null : decodeUtf8(bytes);
  if (answer === null || answer.length === 0) {
    throw new Error("saved wait answer is not readable text");
  }
  return answer;
}

export interface ReconciliationMutation extends MutationBase {
  kind: "reconciliation";
  content_type: "application/json";
  workflow_revision_hash: string;
  node_id: string;
  request_base64: string;
  request_hash: string;
  result_hash: string | null;
}

export type ReconciliationDeterminationInput =
  | { type: "operator_found"; effect_id: string; result_base64: string }
  | { type: "operator_authoritative_absence" };

export type ReconciliationCommand = {
  command_id: string;
  expected_intent_state_version: number;
  actor: string;
  evidence: string;
  determination:
    | { type: "operator_found"; effect_id: string; result_base64: string }
    | { type: "operator_authoritative_absence" };
};

export function createReconcileCommandId(): string {
  return `reconcile-${globalThis.crypto.randomUUID()}`;
}

export async function reconciliationMutation(
  publicRunReference: string,
  workflowRevisionHash: string,
  nodeId: string,
  requestBase64: string,
  requestHash: string,
  expectedIntentStateVersion: number,
  commandId: string,
  actor: string,
  evidence: string,
  determination: ReconciliationDeterminationInput
): Promise<ReconciliationMutation> {
  if (
    decodePublicRunReference(publicRunReference) === null ||
    !digestPattern.test(workflowRevisionHash) ||
    nodeId.length === 0 ||
    commandId.length === 0 ||
    actor.trim().length === 0 ||
    evidence.trim().length === 0 ||
    !Number.isSafeInteger(expectedIntentStateVersion) ||
    expectedIntentStateVersion < 0
  ) {
    throw new Error("invalid reconciliation identity or accountable evidence");
  }
  const request = decodeCanonicalBase64(requestBase64);
  if (request === null || (await sha256Hex(request)) !== requestHash) {
    throw new Error("reconciliation request hash differs from its exact bytes");
  }
  let commandDetermination: ReconciliationCommand["determination"];
  let resultHash: string | null;
  if (determination.type === "operator_found") {
    if (determination.effect_id.trim().length === 0) {
      throw new Error("a found effect requires an effect identity");
    }
    const result = decodeCanonicalBase64(determination.result_base64);
    if (result === null) {
      throw new Error("a found result must be canonical standard base64");
    }
    commandDetermination = {
      type: "operator_found",
      effect_id: determination.effect_id,
      result_base64: determination.result_base64
    };
    resultHash = await sha256Hex(result);
  } else {
    commandDetermination = { type: "operator_authoritative_absence" };
    resultHash = null;
  }
  const command: ReconciliationCommand = {
    command_id: commandId,
    expected_intent_state_version: expectedIntentStateVersion,
    actor,
    evidence,
    determination: commandDetermination
  };
  return {
    mutation_id: `reconciliation:${publicRunReference}:${commandId}`,
    kind: "reconciliation",
    target: `/atelier/api/v1/runs/${publicRunReference}/reconciliations`,
    content_type: "application/json",
    body_base64: encodeBase64(new TextEncoder().encode(JSON.stringify(command))),
    workflow_revision_hash: workflowRevisionHash,
    node_id: nodeId,
    request_base64: requestBase64,
    request_hash: requestHash,
    result_hash: resultHash
  };
}

export function reconciliationCommand(mutation: ReconciliationMutation): ReconciliationCommand {
  const value = requireJsonBody(mutation.body_base64);
  return value as ReconciliationCommand;
}

export type MutationEnvelope =
  | PublishMutation
  | StartMutation
  | WaitMutation
  | ReconciliationMutation;

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
  | (RequestBoundEvidence & { type: "reconciliation_response" })
  | {
      type: "wait_answered";
      public_run_reference: string;
      workflow_revision_hash: string;
      node_id: string;
      answer: string;
      answer_hash: string;
    }
  | {
      type: "reconciliation_resolved";
      public_run_reference: string;
      workflow_revision_hash: string;
      node_id: string;
      command_id: string;
      request_hash: string;
      effect_id: string;
      confirmation_source: "OPERATOR_FOUND" | "OPERATOR_AUTHORIZED_EXECUTION";
      result_base64: string;
      result_hash: string;
    };

const storageKey = "atelier2.mutation-journal.v1";
const digestPattern = /^[0-9a-f]{64}$/;
const canonicalIntegerPattern = /^(?:0|-?[1-9][0-9]*)$/;

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

  async get(mutationId: string): Promise<JournalEntry | null> {
    return (await this.entries()).find((entry) => entry.mutation_id === mutationId) ?? null;
  }

  async entries(): Promise<JournalEntry[]> {
    const stored = this.storage.getItem(storageKey);
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
    if (entry === undefined || !(await evidenceMatches(entry, evidence))) {
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

  private write(entries: JournalEntry[]): void {
    if (entries.length === 0) {
      this.storage.removeItem(storageKey);
    } else {
      this.storage.setItem(storageKey, JSON.stringify(entries));
    }
  }
}

async function requireJournalEntry(value: unknown): Promise<JournalEntry> {
  if (!isRecord(value) || (value.delivery !== "prepared" && value.delivery !== "uncertain")) {
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
    case "reconciliation":
      await requireReconciliation(envelope as ReconciliationMutation);
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

function requireStartBody(bodyBase64: string):
  | { run_id: string; workflow_revision_hash: string }
  | { workflow_format_version: 2; run_id: string; workflow_revision_hash: string; agent_bindings: StartAgentBinding[] }
  | {
      workflow_format_version: 3;
      run_id: string;
      workflow_revision_hash: string;
      agent_bindings: StartAgentBinding[];
      orders: StartOrder[];
    } {
  const body = requireJsonBody(bodyBase64);
  const isV3 = body.workflow_format_version === 3;
  const isV2 = body.workflow_format_version === 2;
  requireExactKeys(
    body,
    isV3
      ? ["workflow_format_version", "run_id", "workflow_revision_hash", "agent_bindings", "orders"]
      : isV2
        ? ["workflow_format_version", "run_id", "workflow_revision_hash", "agent_bindings"]
        : ["run_id", "workflow_revision_hash"]
  );
  if (
    typeof body.run_id !== "string" ||
    body.run_id.length === 0 ||
    typeof body.workflow_revision_hash !== "string" ||
    !digestPattern.test(body.workflow_revision_hash)
  ) throw new Error("invalid start mutation body");
  if (!isV2 && !isV3) return { run_id: body.run_id, workflow_revision_hash: body.workflow_revision_hash };
  const agentBindings = requireStartAgentBindings(body.agent_bindings);
  if (!isV3) {
    return {
      workflow_format_version: 2,
      run_id: body.run_id,
      workflow_revision_hash: body.workflow_revision_hash,
      agent_bindings: agentBindings
    };
  }
  return {
    workflow_format_version: 3,
    run_id: body.run_id,
    workflow_revision_hash: body.workflow_revision_hash,
    agent_bindings: agentBindings,
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
    requireExactKeys(entry, ["name", "value"]);
    if (
      typeof entry.name !== "string" ||
      entry.name.length === 0 ||
      names.has(entry.name) ||
      typeof entry.value !== "string" ||
      entry.value.length === 0
    ) {
      throw new Error("invalid start mutation order");
    }
    names.add(entry.name);
    orders.push({ name: entry.name, value: entry.value });
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
  requireExactKeys(body, ["revision_hash", "node_id", "answer_base64"]);
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
    typeof body.revision_hash !== "string" ||
    !digestPattern.test(body.revision_hash) ||
    envelope.workflow_revision_hash !== body.revision_hash ||
    typeof body.node_id !== "string" ||
    body.node_id.length === 0 ||
    envelope.node_id !== body.node_id ||
    envelope.answer_base64 !== body.answer_base64 ||
    answer === null ||
    answer.length === 0 ||
    envelope.mutation_id !== waitMutationId(publicReference, body.node_id)
  ) {
    throw new Error("invalid wait mutation envelope");
  }
  if (answerBytes === null || (await sha256Hex(answerBytes)) !== envelope.answer_hash) {
    throw new Error("wait answer identity differs from its exact bytes");
  }
}

async function requireReconciliation(envelope: ReconciliationMutation): Promise<void> {
  requireExactKeys(envelope, envelopeKeys(envelope));
  const route = /^\/atelier\/api\/v1\/runs\/(run1\.[A-Za-z0-9_-]+)\/reconciliations$/.exec(
    envelope.target
  );
  const publicReference = route?.[1];
  const body = requireJsonBody(envelope.body_base64);
  requireExactKeys(body, [
    "command_id",
    "expected_intent_state_version",
    "actor",
    "evidence",
    "determination"
  ]);
  const determination = body.determination;
  if (!isRecord(determination) || typeof determination.type !== "string") {
    throw new Error("invalid reconciliation determination");
  }
  if (determination.type === "operator_found") {
    requireExactKeys(determination, ["type", "effect_id", "result_base64"]);
    if (
      typeof determination.effect_id !== "string" ||
      determination.effect_id.length === 0 ||
      typeof determination.result_base64 !== "string" ||
      decodeCanonicalBase64(determination.result_base64) === null ||
      envelope.result_hash === null ||
      !digestPattern.test(envelope.result_hash)
    ) {
      throw new Error("invalid operator-found determination");
    }
    const result = decodeCanonicalBase64(determination.result_base64 as string);
    if (result === null || (await sha256Hex(result)) !== envelope.result_hash) {
      throw new Error("found result identity differs from its exact bytes");
    }
  } else if (determination.type === "operator_authoritative_absence") {
    requireExactKeys(determination, ["type"]);
    if (envelope.result_hash !== null) {
      throw new Error("authoritative absence must not claim a result hash");
    }
  } else {
    throw new Error("invalid reconciliation determination");
  }
  if (
    envelope.content_type !== "application/json" ||
    publicReference === undefined ||
    decodePublicRunReference(publicReference) === null ||
    typeof body.command_id !== "string" ||
    body.command_id.length === 0 ||
    !Number.isSafeInteger(body.expected_intent_state_version) ||
    (body.expected_intent_state_version as number) < 0 ||
    typeof body.actor !== "string" ||
    body.actor.length === 0 ||
    typeof body.evidence !== "string" ||
    body.evidence.length === 0 ||
    typeof envelope.workflow_revision_hash !== "string" ||
    !digestPattern.test(envelope.workflow_revision_hash) ||
    typeof envelope.node_id !== "string" ||
    envelope.node_id.length === 0 ||
    typeof envelope.request_base64 !== "string" ||
    decodeCanonicalBase64(envelope.request_base64) === null ||
    typeof envelope.request_hash !== "string" ||
    !digestPattern.test(envelope.request_hash) ||
    envelope.mutation_id !== `reconciliation:${publicReference}:${body.command_id}`
  ) {
    throw new Error("invalid reconciliation mutation envelope");
  }
  const request = decodeCanonicalBase64(envelope.request_base64);
  if (request === null || (await sha256Hex(request)) !== envelope.request_hash) {
    throw new Error("reconciliation request hash differs from its exact bytes");
  }
}

async function evidenceMatches(
  entry: JournalEntry,
  evidence: MutationEvidence
): Promise<boolean> {
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
    case "reconciliation":
      if (evidence.type === "reconciliation_response") {
        return evidence.status === 200 && requestEvidenceMatches(entry, evidence);
      }
      return (
        evidence.type === "reconciliation_resolved" &&
        (await reconciliationEvidenceMatches(entry, evidence))
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
    evidence.workflow_revision_hash === body.revision_hash &&
    evidence.node_id === body.node_id &&
    evidence.answer === answer &&
    evidence.answer_hash === entry.answer_hash
  );
}

async function reconciliationEvidenceMatches(
  entry: ReconciliationMutation,
  evidence: Extract<MutationEvidence, { type: "reconciliation_resolved" }>
): Promise<boolean> {
  const evidenceResult = decodeCanonicalBase64(evidence.result_base64);
  if (
    evidenceResult === null ||
    !digestPattern.test(evidence.result_hash) ||
    (await sha256Hex(evidenceResult)) !== evidence.result_hash
  ) {
    return false;
  }
  const body = requireJsonBody(entry.body_base64);
  const determination = body.determination as Record<string, unknown>;
  const sourceMatches =
    (determination.type === "operator_found" &&
      evidence.confirmation_source === "OPERATOR_FOUND" &&
      evidence.effect_id === determination.effect_id &&
      evidence.result_base64 === determination.result_base64 &&
      evidence.result_hash === entry.result_hash) ||
    (determination.type === "operator_authoritative_absence" &&
      evidence.confirmation_source === "OPERATOR_AUTHORIZED_EXECUTION");
  return (
    evidence.public_run_reference ===
      publicReferenceFromTarget(entry.target, "reconciliations") &&
    evidence.workflow_revision_hash === entry.workflow_revision_hash &&
    evidence.node_id === entry.node_id &&
    evidence.command_id === body.command_id &&
    evidence.request_hash === entry.request_hash &&
    sourceMatches
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
        "answer_base64",
        "answer_hash"
      ];
    case "reconciliation":
      return [
        ...common,
        "workflow_revision_hash",
        "node_id",
        "request_base64",
        "request_hash",
        "result_hash"
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
