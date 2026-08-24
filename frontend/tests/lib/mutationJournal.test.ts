import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  MutationJournal,
  cancelMutation,
  startMutationV3,
  type MutationEnvelope,
  type MutationEvidence
} from "../../src/lib/mutationJournal";
import { utf8Base64 } from "../support/exactBytes";

const revisionHash = "5e828c8d522a41e966cd17b8172ede0d954f44be653f832cd4f9dc9e8271fb9b";
const requestHash = "1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047";
const answerHash = "4523540f1504cd17100c4835e85b7eefd49911580f8efff0599a8f283be6b9e3";
const emptyResultHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const xResultHash = "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881";
const publicReference = "run1.cnVuLTE";

describe("MutationJournal exact transport truth", () => {
  beforeEach(() => sessionStorage.clear());

  it.each([publish(), start(), wait(), reconciliation(), cancel()])(
    "retains exact Unicode/raw body bytes for $kind across uncertain reload",
    async (envelope) => {
      const journal = new MutationJournal(sessionStorage);
      const prepared = await journal.prepare(envelope);
      await journal.markUncertain(prepared.mutation_id);

      expect(await new MutationJournal(sessionStorage).get(prepared.mutation_id)).toEqual({
        ...prepared,
        delivery: "uncertain"
      });
    }
  );

  it("refuses kind-route-media-body and identity inconsistencies", async () => {
    const invalid = [
      { ...publish(), target: "/atelier/api/v1/runs" },
      { ...publish(), content_type: "application/json" },
      { ...start(), mutation_id: "start:other" },
      { ...start(), body_base64: utf8Base64('{"run_id":"other","workflow_revision_hash":"' + revisionHash + '"}') },
      { ...wait(), target: "/atelier/api/v1/runs/run1.b3RoZXI/answers" },
      { ...wait(), body_base64: utf8Base64(waitBody("other")) },
      { ...reconciliation(), mutation_id: `reconciliation:${publicReference}:other` },
      { ...reconciliation(), body_base64: utf8Base64(reconciliationBody("other")) },
      { ...reconciliation(), node_id: 42 as unknown as string },
      { ...cancel(), mutation_id: `cancel:${publicReference}:other` },
      { ...cancel(), idempotency_key: "another-key" },
      { ...cancel(), target: `/atelier/api/v1/runs/${publicReference}/answers` }
    ];
    for (const envelope of invalid) {
      await expect(
        new MutationJournal(sessionStorage).prepare(envelope as MutationEnvelope)
      ).rejects.toThrow();
    }
  });

  it("rejects corrupt stored JSON, schema, and duplicate ids", async () => {
    sessionStorage.setItem("atelier2.mutation-journal.v1", "{");
    await expect(new MutationJournal(sessionStorage).entries()).rejects.toThrow(/valid JSON/);
    sessionStorage.setItem("atelier2.mutation-journal.v1", JSON.stringify([{ ...start(), delivery: "prepared", extra: true }]));
    await expect(new MutationJournal(sessionStorage).entries()).rejects.toThrow(/unknown fields/);
    sessionStorage.setItem(
      "atelier2.mutation-journal.v1",
      JSON.stringify([
        { ...start(), delivery: "prepared" },
        { ...start(), delivery: "uncertain" }
      ])
    );
    await expect(new MutationJournal(sessionStorage).entries()).rejects.toThrow(/duplicate/);
  });

  it("rejects stored byte/hash identity corruption", async () => {
    for (const corrupt of [
      { ...publish(), revision_hash: "d".repeat(64), mutation_id: `publish:${"d".repeat(64)}` },
      { ...wait(), answer_hash: "d".repeat(64) },
      { ...reconciliation(), request_hash: "d".repeat(64) },
      { ...reconciliation(), result_hash: "d".repeat(64) }
    ]) {
      sessionStorage.setItem(
        "atelier2.mutation-journal.v1",
        JSON.stringify([{ ...corrupt, delivery: "prepared" }])
      );
      await expect(new MutationJournal(sessionStorage).entries()).rejects.toThrow(/bytes|document/);
    }
  });

  it("refuses to reuse one mutation identity for different exact bytes", async () => {
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(publish());
    await expect(
      journal.prepare({ ...publish(), body_base64: utf8Base64("different") })
    ).rejects.toThrow();
  });

  it("retains wait and reconciliation through 202 and unrelated durable evidence", async () => {
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(wait());
    await journal.prepare(reconciliation());

    expect(await journal.resolve(wait().mutation_id, httpEvidence(wait(), 202))).toBe(false);
    expect(
      await journal.resolve(wait().mutation_id, {
        type: "wait_answered",
        public_run_reference: publicReference,
        workflow_revision_hash: revisionHash,
        node_id: "other",
        answer: "17",
        answer_hash: answerHash
      })
    ).toBe(false);
    expect(
      await journal.resolve(reconciliation().mutation_id, {
        type: "reconciliation_resolved",
        public_run_reference: publicReference,
        workflow_revision_hash: revisionHash,
        node_id: "action",
        command_id: "other",
        request_hash: requestHash,
        effect_id: "effect",
        confirmation_source: "OPERATOR_FOUND",
        result_base64: "",
        result_hash: emptyResultHash
      })
    ).toBe(false);
    expect(await journal.get(wait().mutation_id)).not.toBeNull();
    expect(await journal.get(reconciliation().mutation_id)).not.toBeNull();
  });

  it("clears each kind only with matching kind-specific proof", async () => {
    const scenarios: Array<[MutationEnvelope, MutationEvidence]> = [
      [
        publish(),
        {
          type: "publication_response",
          status: 201,
          target: publish().target,
          request_body_base64: publish().body_base64,
          revision_hash: revisionHash,
          document_base64: publish().body_base64
        }
      ],
      [
        start(),
        {
          type: "start_response",
          status: 201,
          target: start().target,
          request_body_base64: start().body_base64,
          run_id: "run-1",
          public_run_reference: publicReference,
          workflow_revision_hash: revisionHash
        }
      ],
      [
        wait(),
        {
          type: "wait_answered",
          public_run_reference: publicReference,
          workflow_revision_hash: revisionHash,
          node_id: "wait",
          answer: "17",
          answer_hash: answerHash
        }
      ],
      [
        reconciliation(),
        {
          type: "reconciliation_resolved",
          public_run_reference: publicReference,
          workflow_revision_hash: revisionHash,
          node_id: "action",
          command_id: "command-1",
          request_hash: requestHash,
          effect_id: "effect",
          confirmation_source: "OPERATOR_FOUND",
          result_base64: "",
          result_hash: emptyResultHash
        }
      ]
    ];
    for (const [envelope, evidence] of scenarios) {
      sessionStorage.clear();
      const journal = new MutationJournal(sessionStorage);
      await journal.prepare(envelope);
      expect(await journal.resolve(envelope.mutation_id, evidence)).toBe(true);
      expect(await journal.get(envelope.mutation_id)).toBeNull();
    }
  });

  it("derives an authorized execution result hash before clearing", async () => {
    const envelope = absenceReconciliation();
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(envelope);
    const evidence: MutationEvidence = {
      type: "reconciliation_resolved",
      public_run_reference: publicReference,
      workflow_revision_hash: revisionHash,
      node_id: "action",
      command_id: "command-absence",
      request_hash: requestHash,
      effect_id: "executed-effect",
      confirmation_source: "OPERATOR_AUTHORIZED_EXECUTION",
      result_base64: "eA==",
      result_hash: emptyResultHash
    };

    expect(await journal.resolve(envelope.mutation_id, evidence)).toBe(false);
    expect(await journal.get(envelope.mutation_id)).not.toBeNull();
    expect(await journal.resolve(envelope.mutation_id, { ...evidence, result_hash: xResultHash })).toBe(true);
  });

  it("retains Found reconciliation until durable evidence binds the exact effect", async () => {
    const envelope = reconciliation();
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(envelope);
    const evidence: MutationEvidence = {
      type: "reconciliation_resolved",
      public_run_reference: publicReference,
      workflow_revision_hash: revisionHash,
      node_id: "action",
      command_id: "command-1",
      request_hash: requestHash,
      effect_id: "other-effect",
      confirmation_source: "OPERATOR_FOUND",
      result_base64: "",
      result_hash: emptyResultHash
    };

    expect(await journal.resolve(envelope.mutation_id, evidence)).toBe(false);
    expect(await journal.get(envelope.mutation_id)).not.toBeNull();
    expect(await journal.resolve(envelope.mutation_id, { ...evidence, effect_id: "effect" })).toBe(true);
  });

  it.each([201, 301, 400, 500, 503])(
    "retains exact wait and reconciliation requests after undocumented or failed HTTP %i",
    async (status) => {
      const journal = new MutationJournal(sessionStorage);
      for (const envelope of [wait(), reconciliation()]) {
        await journal.prepare(envelope);
        expect(await journal.resolve(envelope.mutation_id, httpEvidence(envelope, status))).toBe(false);
        expect(await journal.get(envelope.mutation_id)).not.toBeNull();
      }
    }
  );

  it.each([wait(), reconciliation()])(
    "clears an exact $kind request after the documented HTTP 200",
    async (envelope) => {
      const journal = new MutationJournal(sessionStorage);
      await journal.prepare(envelope);

      expect(await journal.resolve(envelope.mutation_id, httpEvidence(envelope, 200))).toBe(true);
      expect(await journal.get(envelope.mutation_id)).toBeNull();
    }
  );

  it("clears an exact cancel on the terminal 200 but keeps it on the 202-accepted reply", async () => {
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(cancel());
    expect(await journal.resolve(cancel().mutation_id, httpEvidence(cancel(), 202))).toBe(false);
    expect(await journal.get(cancel().mutation_id)).not.toBeNull();

    expect(await journal.resolve(cancel().mutation_id, httpEvidence(cancel(), 200))).toBe(true);
    expect(await journal.get(cancel().mutation_id)).toBeNull();
  });

  it("retains a V3 start that carries the exact order bytes", async () => {
    const envelope = startMutationV3(
      "run-1",
      revisionHash,
      [{ role: "cook", agent_configuration_revision_hash: "c".repeat(64) }],
      [{ name: "portions", value: '{"portions": 7}' }]
    );
    const journal = new MutationJournal(sessionStorage);
    await journal.prepare(envelope);
    await journal.markUncertain(envelope.mutation_id);

    expect(await new MutationJournal(sessionStorage).get(envelope.mutation_id)).toEqual({
      ...envelope,
      delivery: "uncertain"
    });
    const body = JSON.parse(globalThis.atob(envelope.body_base64)) as {
      orders: Array<{ name: string; value: string }>;
    };
    expect(body.orders).toEqual([{ name: "portions", value: '{"portions": 7}' }]);
  });

  it("refuses a V3 start whose order is empty or duplicated", async () => {
    const journal = new MutationJournal(sessionStorage);
    const bound = [{ role: "cook", agent_configuration_revision_hash: "c".repeat(64) }];
    await expect(
      journal.prepare(
        startMutationV3("run-1", revisionHash, bound, [{ name: "portions", value: "" }])
      )
    ).rejects.toThrow(/invalid start mutation order/);
    await expect(
      journal.prepare(
        startMutationV3("run-1", revisionHash, bound, [
          { name: "portions", value: "1" },
          { name: "portions", value: "2" }
        ])
      )
    ).rejects.toThrow(/invalid start mutation order/);
  });

  it("fails loud when storage set or remove fails", async () => {
    const setFailure = new Error("set failed");
    const setStorage = storage({ setItem: vi.fn(() => { throw setFailure; }) });
    await expect(new MutationJournal(setStorage).prepare(start())).rejects.toThrow(setFailure);

    const removeFailure = new Error("remove failed");
    const removeStorage = storage({
      getItem: vi.fn(() => JSON.stringify([{ ...start(), delivery: "prepared" }])),
      removeItem: vi.fn(() => { throw removeFailure; })
    });
    await expect(
      new MutationJournal(removeStorage).discard(start().mutation_id)
    ).rejects.toThrow(removeFailure);
  });
});

function publish(): MutationEnvelope {
  return {
    mutation_id: `publish:${revisionHash}`,
    kind: "publish",
    target: "/atelier/api/v1/workflow-revisions",
    content_type: "application/yaml",
    body_base64: utf8Base64("job: Grüße 東京\n"),
    revision_hash: revisionHash
  };
}

function start(): MutationEnvelope {
  return {
    mutation_id: "start:run-1",
    kind: "start",
    target: "/atelier/api/v1/runs",
    content_type: "application/json",
    body_base64: utf8Base64(
      JSON.stringify({ run_id: "run-1", workflow_revision_hash: revisionHash })
    )
  };
}

function wait(): MutationEnvelope {
  return {
    mutation_id: `wait:${publicReference}:wait`,
    kind: "wait",
    target: `/atelier/api/v1/runs/${publicReference}/answers`,
    content_type: "application/json",
    body_base64: utf8Base64(waitBody("wait")),
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    node_id: "wait",
    answer_base64: "MTc=",
    answer_hash: answerHash
  };
}

function waitBody(node_id: string): string {
  return JSON.stringify({ workflow_revision_hash: revisionHash, node_id, answer_base64: "MTc=" });
}

function reconciliation(): Extract<MutationEnvelope, { kind: "reconciliation" }> {
  return {
    mutation_id: `reconciliation:${publicReference}:command-1`,
    kind: "reconciliation",
    target: `/atelier/api/v1/runs/${publicReference}/reconciliations`,
    content_type: "application/json",
    body_base64: utf8Base64(reconciliationBody("command-1")),
    workflow_revision_hash: revisionHash,
    node_id: "action",
    request_base64: "cmVxdWVzdA==",
    request_hash: requestHash,
    result_hash: emptyResultHash
  };
}

function reconciliationBody(command_id: string): string {
  return JSON.stringify({
    command_id,
    expected_intent_state_version: 3,
    actor: "operator",
    evidence: "inspected",
    determination: { type: "operator_found", effect_id: "effect", result_base64: "" }
  });
}

function absenceReconciliation(): MutationEnvelope {
  const commandId = "command-absence";
  return {
    ...reconciliation(),
    mutation_id: `reconciliation:${publicReference}:${commandId}`,
    body_base64: utf8Base64(
      JSON.stringify({
        command_id: commandId,
        expected_intent_state_version: 3,
        actor: "operator",
        evidence: "inspected",
        determination: { type: "operator_authoritative_absence" }
      })
    ),
    result_hash: null
  };
}

function cancel(): Extract<MutationEnvelope, { kind: "cancel" }> {
  return cancelMutation(publicReference, requestHash, "cancel-key-1");
}

function httpEvidence(envelope: MutationEnvelope, status: number): MutationEvidence {
  const type =
    envelope.kind === "wait"
      ? "wait_response"
      : envelope.kind === "cancel"
        ? "cancel_response"
        : "reconciliation_response";
  return {
    type,
    status,
    target: envelope.target,
    request_body_base64: envelope.body_base64
  };
}

function storage(overrides: Partial<Storage>): Storage {
  return {
    getItem: vi.fn(() => null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
    key: vi.fn(() => null),
    length: 0,
    ...overrides
  };
}
