import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  createCockpitApi,
  type CockpitApi,
  type Run,
  type RunEventHandlers,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import {
  MutationJournal,
  reconciliationCommand,
  type ReconciliationMutation
} from "../../src/lib/mutationJournal";

const digest = "a".repeat(64);
const requestHash = "1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047";
const emptyResultHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const publicReference = "run1.cnVu";

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
});

afterEach(() => cleanup());

describe("Phase 5 reconciliation control", () => {
  it("reconcile_found_keeps_exact_identity_and_result", async () => {
    const journal = new MutationJournal(sessionStorage);
    const feed = new FakeFeed();
    const reconcile = vi
      .fn()
      .mockRejectedValueOnce(new CockpitRequestError("The connection ended without a response."))
      .mockResolvedValueOnce({ status: 202, value: reconciliationRun(pendingFoundCommand()) });
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(reconciliationRun())
      .mockResolvedValueOnce(reconciliationRun(pendingFoundCommand()))
      .mockResolvedValue(afterReconciliationRun());
    const createCommandId = vi.fn(() => "command-found");

    render(App, {
      props: {
        cockpitApi: api({ reconcile, getRun, openRunEvents: feed.open }),
        mutationJournal: journal,
        createReconcileCommandId: createCommandId
      }
    });

    expect(await screen.findByRole("heading", { name: "Decision needed" })).toBeTruthy();
    expect(screen.getByText("effect-key")).toBeTruthy();
    expect(screen.getByText(requestHash)).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    await fireEvent.input(screen.getByLabelText("Actor"), { target: { value: "Felix" } });
    await fireEvent.input(screen.getByLabelText("Evidence"), {
      target: { value: "Inspected the exact destination" }
    });
    await fireEvent.input(screen.getByLabelText("Effect ID"), {
      target: { value: "effect-empty" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Resolve" }));

    expect(await screen.findByRole("heading", { name: "Decision uncertain" })).toBeTruthy();
    expect(reconcile).toHaveBeenCalledTimes(1);
    const first = reconcile.mock.calls[0]?.[0] as ReconciliationMutation;
    expect(reconciliationCommand(first)).toEqual({
      command_id: "command-found",
      expected_intent_state_version: 7,
      actor: "Felix",
      evidence: "Inspected the exact destination",
      determination: {
        type: "operator_found",
        effect_id: "effect-empty",
        result_base64: ""
      }
    });
    expect(first.result_hash).toBe(emptyResultHash);
    expect(createCommandId).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Empty result")).toBeTruthy();
    expect(screen.getByRole("article", { name: "action — Working" })).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(reconcile).toHaveBeenCalledTimes(2);
    expect(reconcile.mock.calls[1]?.[0]).toMatchObject(first);
    expect(reconcile.mock.calls[1]?.[0].body_base64).toBe(first.body_base64);
    expect(await screen.findByRole("heading", { name: "Decision pending" })).toBeTruthy();
    expect(await journal.get(first.mutation_id)).toMatchObject({ ...first, delivery: "uncertain" });

    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    feed.handlers?.event(JSON.stringify(reconciliationRequired(2)));
    expect(await screen.findByRole("heading", { name: "Decision pending" })).toBeTruthy();
    expect(await journal.get(first.mutation_id)).not.toBeNull();
    feed.handlers?.event(JSON.stringify(reconciliationResolved(3, "command-found", "OPERATOR_FOUND")));

    await waitFor(async () => expect(await journal.get(first.mutation_id)).toBeNull());
    expect(screen.queryByRole("heading", { name: /Decision/ })).toBeNull();
    expect(screen.getByRole("article", { name: "action — Working" })).toBeTruthy();
  });

  it("keeps a durable reconciliation authoritative when its event arrives before the 202 response", async () => {
    const journal = new MutationJournal(sessionStorage);
    const feed = new FakeFeed();
    let acceptDecision!: (result: { status: number; value: Run }) => void;
    const reconcile = vi.fn(
      () => new Promise<{ status: number; value: Run }>((resolve) => { acceptDecision = resolve; })
    );
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(reconciliationRun())
      .mockResolvedValueOnce(reconciliationRun())
      .mockResolvedValue(afterReconciliationRun());
    render(App, {
      props: {
        cockpitApi: api({ reconcile, getRun, openRunEvents: feed.open }),
        mutationJournal: journal,
        createReconcileCommandId: () => "command-race"
      }
    });
    await screen.findByRole("heading", { name: "Decision needed" });
    await fireEvent.input(screen.getByLabelText("Actor"), { target: { value: "Felix" } });
    await fireEvent.input(screen.getByLabelText("Evidence"), { target: { value: "Exact check" } });
    await fireEvent.input(screen.getByLabelText("Effect ID"), { target: { value: "effect-empty" } });
    await fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    expect(await screen.findByRole("heading", { name: "Sending decision" })).toBeTruthy();
    await waitFor(() => expect(reconcile).toHaveBeenCalledTimes(1));

    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    feed.handlers?.event(JSON.stringify(reconciliationRequired(2)));
    feed.handlers?.event(JSON.stringify(reconciliationResolved(3, "command-race", "OPERATOR_FOUND")));
    acceptDecision({ status: 202, value: reconciliationRun() });

    await waitFor(async () => expect(await journal.entries()).toEqual([]));
    expect(screen.queryByRole("heading", { name: /Decision/ })).toBeNull();
    expect(screen.getByRole("article", { name: "action — Working" })).toBeTruthy();
  });

  it("reconcile_absent_requires_execute_confirmation", async () => {
    const journal = new MutationJournal(sessionStorage);
    let rejectFirst!: (reason: unknown) => void;
    const reconcile = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise((_resolve, reject) => { rejectFirst = reject; })
      )
      .mockResolvedValueOnce({ status: 202, value: reconciliationRun(pendingAbsentCommand()) });

    render(App, {
      props: {
        cockpitApi: api({ reconcile }),
        mutationJournal: journal,
        createReconcileCommandId: () => "command-absent"
      }
    });

    await screen.findByRole("heading", { name: "Decision needed" });
    await fireEvent.input(screen.getByLabelText("Actor"), { target: { value: "Felix" } });
    await fireEvent.input(screen.getByLabelText("Evidence"), {
      target: { value: "Destination has no matching effect" }
    });
    await fireEvent.click(screen.getByLabelText("Absent"));
    const review = screen.getByRole("button", { name: "Review" });
    await fireEvent.click(review);

    expect(reconcile).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Execute this exact effect?" })).toBeTruthy();
    expect(screen.getByText("Atelier will execute the exact request once.")).toBeTruthy();
    await fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(review);
    expect(reconcile).not.toHaveBeenCalled();

    await fireEvent.click(review);
    await fireEvent.click(screen.getByRole("button", { name: "Execute" }));

    const sending = await screen.findByRole("heading", { name: "Sending decision" });
    expect(document.activeElement).toBe(sending);
    await waitFor(() => expect(reconcile).toHaveBeenCalledTimes(1));
    rejectFirst(new CockpitRequestError("The response was lost."));
    expect(await screen.findByRole("heading", { name: "Decision uncertain" })).toBeTruthy();
    expect(reconcile).toHaveBeenCalledTimes(1);
    const first = reconcile.mock.calls[0]?.[0] as ReconciliationMutation;
    expect(reconciliationCommand(first)).toEqual({
      command_id: "command-absent",
      expected_intent_state_version: 7,
      actor: "Felix",
      evidence: "Destination has no matching effect",
      determination: { type: "operator_authoritative_absence" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(reconcile).toHaveBeenCalledTimes(2);
    expect(reconcile.mock.calls[1]?.[0]).toMatchObject(first);
    expect(reconcile.mock.calls[1]?.[0].body_base64).toBe(first.body_base64);
    expect(await screen.findByRole("heading", { name: "Decision pending" })).toBeTruthy();
  });

  it("shows reconciliation only for its exact durable state and projects server-owned pending as Working", async () => {
    const first = render(App, {
      props: { cockpitApi: api({ getRun: vi.fn(async () => startedRun()) }) }
    });
    await screen.findByRole("heading", { name: "Run run" });
    expect(screen.queryByRole("heading", { name: /Decision/ })).toBeNull();
    expect(screen.queryByLabelText("Actor")).toBeNull();

    first.unmount();
    render(App, {
      props: {
        cockpitApi: api({ getRun: vi.fn(async () => reconciliationRun(pendingFoundCommand())) })
      }
    });

    expect(await screen.findByRole("heading", { name: "Decision pending" })).toBeTruthy();
    expect(screen.getByText("Felix")).toBeTruthy();
    expect(screen.getByText("Inspected the exact destination")).toBeTruthy();
    expect(screen.getByText("effect-empty")).toBeTruthy();
    expect(screen.getByText("Empty result")).toBeTruthy();
    expect(screen.queryByLabelText("Actor")).toBeNull();
    expect(screen.getByRole("article", { name: "action — Working" })).toBeTruthy();
  });

  it("sends the journalled reconciliation body as exact bytes to the R2 endpoint", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(reconciliationRun(pendingFoundCommand())), {
        status: 202,
        headers: { "content-type": "application/json" }
      })
    );
    const mutation = await reconciliationMutationForTransport();

    const result = await createCockpitApi(fetcher).reconcile(mutation);

    expect(result.status).toBe(202);
    const [target, init] = fetcher.mock.calls[0] ?? [];
    expect(target).toBe(`/atelier/api/v1/runs/${publicReference}/reconciliations`);
    expect(init?.method).toBe("POST");
    expect(new TextDecoder().decode(init?.body as ArrayBuffer)).toBe(
      JSON.stringify(reconciliationCommand(mutation))
    );
    expect(reconciliationCommand(mutation).determination).toEqual({
      type: "operator_found",
      effect_id: "effect-empty",
      result_base64: "/wA="
    });
    expect(mutation.result_hash).toBe(
      "ea5dbf9596d187e9500f23e9a680109475341cf4e81f7e043f7d97152c10772f"
    );
  });
});

class FakeFeed {
  handlers: RunEventHandlers | null = null;
  open = vi.fn((_publicReference: string, handlers: RunEventHandlers) => {
    this.handlers = handlers;
    return { close: vi.fn() };
  });
}

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return {
    listRuns: vi.fn(async () => ({ items: [], next_after: null })),
    listWorkflowRevisions: vi.fn(async () => ({ items: [], next_after_revision_hash: null })),
    publish: vi.fn(),
    start: vi.fn(),
    answer: vi.fn(),
    reconcile: vi.fn(),
    getRun: vi.fn(async () => reconciliationRun()),
    getWorkflowRevision: vi.fn(async () => revision()),
    openRunEvents: vi.fn(),
    ...overrides
  };
}

function revision(): WorkflowRevisionDetail {
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

function startedRun(): Run {
  return {
    run_id: "run",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    state_version: 1,
    state: "STARTED",
    current_node: revision().graph.nodes[1]!,
    waiting: { type: "NONE" },
    terminal_hash: null,
    latest_event_cursor: "event1.cnVu.1"
  };
}

function reconciliationRun(
  pending_command: Extract<Run["waiting"], { type: "WAITING_RECONCILIATION" }>["pending_command"] = null
): Run {
  return {
    ...startedRun(),
    state_version: pending_command === null ? 2 : 3,
    state: "WAITING_RECONCILIATION",
    waiting: {
      type: "WAITING_RECONCILIATION",
      node_id: "action",
      logical_effect_key: "effect-key",
      request_hash: requestHash,
      request_base64: "cmVxdWVzdA==",
      intent_state_version: 7,
      pending_command
    },
    latest_event_cursor: "event1.cnVu.2"
  };
}

function afterReconciliationRun(): Run {
  return {
    ...startedRun(),
    state_version: 4,
    latest_event_cursor: "event1.cnVu.3"
  };
}

function pendingFoundCommand() {
  return {
    command_id: "command-found",
    actor: "Felix",
    evidence: "Inspected the exact destination",
    state: "PENDING" as const,
    determination: {
      type: "operator_found" as const,
      effect_id: "effect-empty",
      result_base64: ""
    }
  };
}

function pendingAbsentCommand() {
  return {
    command_id: "command-absent",
    actor: "Felix",
    evidence: "Destination has no matching effect",
    state: "PENDING" as const,
    determination: { type: "operator_authoritative_absence" as const }
  };
}

function event(sequence: number, kind: string, fields: Record<string, unknown>) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: sequence === 1 ? "agent" : "action",
    node_execution_id: digest,
    event_hash: digest,
    event: kind,
    ...fields
  };
}

function agentCompleted(sequence: number) {
  return event(sequence, "AGENT_COMPLETED", { output: "candidate", payload_hash: digest });
}

function reconciliationRequired(sequence: number) {
  return event(sequence, "ACTION_RECONCILIATION_REQUIRED", {
    request_base64: "cmVxdWVzdA==",
    request_hash: requestHash
  });
}

function reconciliationResolved(
  sequence: number,
  commandId: string,
  confirmationSource: "OPERATOR_FOUND" | "OPERATOR_AUTHORIZED_EXECUTION"
) {
  return event(sequence, "ACTION_RECONCILIATION_RESOLVED", {
    receipt: {
      logical_effect_key: "effect-key",
      request_hash: requestHash,
      effect_id: confirmationSource === "OPERATOR_FOUND" ? "effect-empty" : "executed-effect",
      result_hash: emptyResultHash,
      result_base64: "",
      confirmation_source: confirmationSource,
      reconcile_command_id: commandId
    }
  });
}

async function reconciliationMutationForTransport(): Promise<ReconciliationMutation> {
  const { reconciliationMutation } = await import("../../src/lib/mutationJournal");
  return reconciliationMutation(
    publicReference,
    digest,
    "action",
    "cmVxdWVzdA==",
    requestHash,
    7,
    "command-found",
    "Felix",
    "Inspected the exact destination",
    { type: "operator_found", effect_id: "effect-empty", result_base64: "/wA=" }
  );
}
