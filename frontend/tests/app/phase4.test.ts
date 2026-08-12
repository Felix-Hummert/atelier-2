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
  waitMutation,
  waitMutationId,
  type WaitMutation
} from "../../src/lib/mutationJournal";

const digest = "a".repeat(64);
const answerHash = "4523540f1504cd17100c4835e85b7eefd49911580f8efff0599a8f283be6b9e3";
const publicReference = "run1.cnVu";

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
});

afterEach(() => cleanup());

describe("Phase 4 Wait control", () => {
  it("wait_ambiguous_retry_is_exact_and_disappears_after_durable_event", async () => {
    const journal = new MutationJournal(sessionStorage);
    const feed = new FakeFeed();
    const answer = vi
      .fn()
      .mockRejectedValueOnce(new CockpitRequestError("The connection ended without a response."))
      .mockResolvedValueOnce({ status: 202, value: waitingRun() });
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValue(afterAnswerRun());

    render(App, {
      props: {
        cockpitApi: api({ answer, getRun, openRunEvents: feed.open }),
        mutationJournal: journal
      }
    });

    const field = await screen.findByLabelText("Integer answer");
    await fireEvent.input(field, { target: { value: "17" } });
    await fireEvent.click(screen.getByRole("button", { name: "Answer" }));

    expect(await screen.findByRole("heading", { name: "Answer uncertain" })).toBeTruthy();
    expect(answer).toHaveBeenCalledTimes(1);
    const firstRequest = answer.mock.calls[0]?.[0] as WaitMutation;
    expect(exactBody(firstRequest)).toBe(
      JSON.stringify({ revision_hash: digest, node_id: "wait", answer_base64: "MTc=" })
    );
    expect(firstRequest.answer_hash).toBe(answerHash);
    expect(screen.getByRole("article", { name: "wait — Working" })).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(answer).toHaveBeenCalledTimes(2);
    expect(answer.mock.calls[1]?.[0]).toMatchObject(firstRequest);
    expect(await screen.findByRole("heading", { name: "Answer pending" })).toBeTruthy();
    expect(await journal.get(firstRequest.mutation_id)).toMatchObject({
      ...firstRequest,
      delivery: "uncertain"
    });

    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    feed.handlers?.event(JSON.stringify(actionCompleted(2)));
    feed.handlers?.event(JSON.stringify(waitingInput(3)));
    feed.handlers?.event(JSON.stringify(waitAnswered(4)));

    await waitFor(async () => expect(await journal.get(firstRequest.mutation_id)).toBeNull());
    expect(screen.queryByRole("heading", { name: /Answer/ })).toBeNull();
    expect(screen.getByRole("article", { name: "wait — Done" })).toBeTruthy();
    expect(screen.getByRole("article", { name: "final — Working" })).toBeTruthy();
  });

  it.each(["", "01", "+1", "-0", " 1", "1 ", "1.0"])(
    "rejects a noncanonical integer without preparing or sending it: %j",
    async (value) => {
      const journal = new MutationJournal(sessionStorage);
      const answer = vi.fn();
      render(App, {
        props: { cockpitApi: api({ answer }), mutationJournal: journal }
      });

      const field = await screen.findByLabelText("Integer answer");
      await fireEvent.input(field, { target: { value } });
      await fireEvent.click(screen.getByRole("button", { name: "Answer" }));

      expect(await screen.findByText("Use one canonical integer.")).toBeTruthy();
      expect(answer).not.toHaveBeenCalled();
      expect(await journal.entries()).toEqual([]);
    }
  );

  it("restores an uncertain exact answer after reload and lets the operator discard it", async () => {
    const journal = new MutationJournal(sessionStorage);
    const mutation = await waitMutation(publicReference, digest, "wait", "17");
    await journal.prepare(mutation);
    await journal.markUncertain(mutation.mutation_id);

    render(App, { props: { cockpitApi: api(), mutationJournal: journal } });

    expect(await screen.findByRole("heading", { name: "Answer uncertain" })).toBeTruthy();
    expect(screen.getByText("17")).toBeTruthy();
    expect(screen.getByRole("article", { name: "wait — Working" })).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(await screen.findByLabelText("Integer answer")).toBeTruthy();
    expect(screen.getByRole("article", { name: "wait — Needs you" })).toBeTruthy();
    expect(await journal.get(waitMutationId(publicReference, "wait"))).toBeNull();
  });

  it("keeps a durable answer authoritative when its event arrives before the 202 response", async () => {
    const journal = new MutationJournal(sessionStorage);
    const feed = new FakeFeed();
    let acceptAnswer!: (result: { status: number; value: Run }) => void;
    const answer = vi.fn(
      () => new Promise<{ status: number; value: Run }>((resolve) => { acceptAnswer = resolve; })
    );
    const getRun = vi.fn().mockResolvedValueOnce(waitingRun()).mockResolvedValue(afterAnswerRun());
    render(App, {
      props: {
        cockpitApi: api({ answer, getRun, openRunEvents: feed.open }),
        mutationJournal: journal
      }
    });
    await fireEvent.input(await screen.findByLabelText("Integer answer"), {
      target: { value: "17" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Answer" }));
    expect(await screen.findByRole("heading", { name: "Sending answer" })).toBeTruthy();

    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    feed.handlers?.event(JSON.stringify(actionCompleted(2)));
    feed.handlers?.event(JSON.stringify(waitingInput(3)));
    feed.handlers?.event(JSON.stringify(waitAnswered(4)));
    acceptAnswer({ status: 202, value: waitingRun() });

    expect(await screen.findByText("started")).toBeTruthy();
    await waitFor(async () => expect(await journal.entries()).toEqual([]));
    expect(screen.queryByRole("heading", { name: /Answer/ })).toBeNull();
    expect(screen.getByRole("article", { name: "wait — Done" })).toBeTruthy();
  });

  it("shows no Wait action unless the authoritative run is WAITING_INPUT", async () => {
    render(App, { props: { cockpitApi: api({ getRun: vi.fn(async () => startedRun()) }) } });

    await screen.findByRole("heading", { name: "Run run" });
    expect(screen.queryByLabelText("Integer answer")).toBeNull();
    expect(screen.queryByRole("button", { name: "Answer" })).toBeNull();
  });

  it("sends the saved exact JSON bytes to the R2 answer endpoint", async () => {
    const mutation = await waitMutation(publicReference, digest, "wait", "17");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(waitingRun()), {
        status: 202,
        headers: { "content-type": "application/json" }
      })
    );

    const result = await createCockpitApi(fetcher).answer(mutation);

    expect(result.status).toBe(202);
    const [target, init] = fetcher.mock.calls[0] ?? [];
    expect(target).toBe(`/atelier/api/v1/runs/${publicReference}/answers`);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ accept: "application/json", "content-type": "application/json" });
    expect(new TextDecoder().decode(init?.body as ArrayBuffer)).toBe(exactBody(mutation));
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
    getRun: vi.fn(async () => waitingRun()),
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
    state_version: 0,
    state: "STARTED",
    current_node: revision().graph.nodes[0]!,
    waiting: { type: "NONE" },
    terminal_hash: null,
    latest_event_cursor: null
  };
}

function waitingRun(): Run {
  return {
    ...startedRun(),
    state_version: 3,
    state: "WAITING_INPUT",
    current_node: revision().graph.nodes[2]!,
    waiting: { type: "WAITING_INPUT", node_id: "wait", answer_type: "integer" },
    latest_event_cursor: "event1.cnVu.3"
  };
}

function afterAnswerRun(): Run {
  return {
    ...startedRun(),
    state_version: 4,
    current_node: revision().graph.nodes[3]!,
    latest_event_cursor: "event1.cnVu.4"
  };
}

function event(sequence: number, node_id: string, kind: string, fields: Record<string, unknown>) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id,
    node_execution_id: digest,
    event_hash: digest,
    event: kind,
    ...fields
  };
}

function agentCompleted(sequence: number) {
  return event(sequence, "agent", "AGENT_COMPLETED", { output: "candidate", payload_hash: digest });
}

function actionCompleted(sequence: number) {
  return event(sequence, "action", "ACTION_COMPLETED", {
    receipt: {
      logical_effect_key: "effect",
      request_hash: digest,
      effect_id: "effect-1",
      result_hash: digest,
      result_base64: "cmVzdWx0",
      confirmation_source: "ADAPTER_EXECUTION",
      reconcile_command_id: null
    }
  });
}

function waitingInput(sequence: number) {
  return event(sequence, "wait", "WAITING_INPUT", { answer_type: "integer" });
}

function waitAnswered(sequence: number) {
  return event(sequence, "wait", "WAIT_ANSWERED", { answer: "17", answer_hash: answerHash });
}

function exactBody(mutation: WaitMutation): string {
  return new TextDecoder().decode(
    Uint8Array.from(atob(mutation.body_base64), (character) => character.charCodeAt(0))
  );
}
