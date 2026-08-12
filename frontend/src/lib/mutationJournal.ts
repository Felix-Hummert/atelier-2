import {
  decodeCanonicalBase64,
  decodePublicRunReference
} from "../api/client";

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

export interface WaitMutation extends MutationBase {
  kind: "wait";
  content_type: "application/json";
  answer_hash: string;
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
  const body = requireJsonBody(envelope.body_base64);
  requireExactKeys(body, ["run_id", "workflow_revision_hash"]);
  if (
    envelope.target !== "/atelier/api/v1/runs" ||
    envelope.content_type !== "application/json" ||
    typeof body.run_id !== "string" ||
    body.run_id.length === 0 ||
    typeof body.workflow_revision_hash !== "string" ||
    !digestPattern.test(body.workflow_revision_hash) ||
    envelope.mutation_id !== `start:${body.run_id}`
  ) {
    throw new Error("invalid start mutation envelope");
  }
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
    typeof body.revision_hash !== "string" ||
    !digestPattern.test(body.revision_hash) ||
    typeof body.node_id !== "string" ||
    body.node_id.length === 0 ||
    answer === null ||
    !canonicalIntegerPattern.test(answer) ||
    envelope.mutation_id !== `wait:${publicReference}:${body.node_id}`
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
      return [...common, "answer_hash"];
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

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const copied = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(copied).set(bytes);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", copied);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join(
    ""
  );
}
