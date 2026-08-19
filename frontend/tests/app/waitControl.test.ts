import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  createCockpitApi,
  type CockpitApi,
  type Run
} from "../../src/api/client";
import {
  MutationJournal,
  waitMutation,
  waitMutationId,
  type WaitMutation
} from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { exactBody } from "../support/exactBytes";
import {
  actionCompleted,
  agentCompleted,
  answerHash,
  answeredRun,
  publicReference,
  revisionHash as digest,
  startedRun,
  waitAnswered,
  waitingInput,
  waitingInputRun,
  workflowRevision as revision
} from "../support/workflowV1";

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
});

afterEach(() => cleanup());

describe("Wait control", () => {
  it("wait_ambiguous_retry_is_exact_and_disappears_after_durable_event", async () => {
    const journal = new MutationJournal(sessionStorage);
    const feed = new FakeRunEventFeed();
    const answer = vi
      .fn()
      .mockRejectedValueOnce(new CockpitRequestError("The connection ended without a response."))
      .mockResolvedValueOnce({ status: 202, value: waitingInputRun() });
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(waitingInputRun())
      .mockResolvedValueOnce(waitingInputRun())
      .mockResolvedValue(answeredRun());

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
    expect(screen.getByRole("alert", { name: "Send uncertain" })).toBeTruthy();
    expect(screen.queryByText("Run unavailable")).toBeNull();
    expect(answer).toHaveBeenCalledTimes(1);
    const firstRequest = answer.mock.calls[0]?.[0] as WaitMutation;
    expect(exactBody(firstRequest)).toBe(
      JSON.stringify({ workflow_revision_hash: digest, node_id: "wait", answer_base64: "MTc=" })
    );
    expect(firstRequest.answer_hash).toBe(answerHash);
    expect(screen.getByRole("article", { name: "wait — Working" })).toBeTruthy();
    expect(screen.getByRole("article", { name: "wait — Working" }).querySelector(".state-mark")?.classList).toContain("state-still");
    expect(screen.getByRole("region", { name: "Answer uncertain" }).querySelector(".human-action-shape-working")).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Retry" })));

    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(answer).toHaveBeenCalledTimes(2);
    expect(answer.mock.calls[1]?.[0]).toMatchObject(firstRequest);
    expect(await screen.findByRole("heading", { name: "Answer pending" })).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Answer pending" })));
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
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId("run-state")));
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

    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText("Integer answer")));
    expect(screen.getByRole("region", { name: "Answer needed" }).querySelector(".human-action-shape-needs")).toBeTruthy();
    expect(screen.getByRole("article", { name: "wait — Needs you" })).toBeTruthy();
    expect(await journal.get(waitMutationId(publicReference, "wait"))).toBeNull();
  });

  it("keeps a durable answer authoritative when its event arrives before the 202 response", async () => {
    const journal = new MutationJournal(sessionStorage);
    const feed = new FakeRunEventFeed();
    let acceptAnswer!: (result: { status: number; value: Run }) => void;
    const answer = vi.fn(
      () => new Promise<{ status: number; value: Run }>((resolve) => { acceptAnswer = resolve; })
    );
    const getRun = vi.fn().mockResolvedValueOnce(waitingInputRun()).mockResolvedValue(answeredRun());
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
    acceptAnswer({ status: 202, value: waitingInputRun() });

    expect(await screen.findByText("started")).toBeTruthy();
    await waitFor(async () => expect(await journal.entries()).toEqual([]));
    expect(screen.queryByRole("heading", { name: /Answer/ })).toBeNull();
    expect(screen.getByRole("article", { name: "wait — Done" })).toBeTruthy();
  });

  it("names a definitively refused answer as failed and hands the form back with nothing pending", async () => {
    const journal = new MutationJournal(sessionStorage);
    const answer = vi.fn().mockRejectedValue(
      new CockpitRequestError("The answer state conflicts with the durable run.", {
        type: "urn:atelier2:problem:v1:answer-state-conflict",
        title: "Answer state conflict",
        status: 409,
        detail: "The durable run is no longer waiting for this answer."
      }, true)
    );
    render(App, { props: { cockpitApi: api({ answer }), mutationJournal: journal } });

    await fireEvent.input(await screen.findByLabelText("Integer answer"), {
      target: { value: "17" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Answer" }));

    const alert = await screen.findByRole("alert", { name: "Send failed" });
    expect(alert.textContent).toContain("The answer state conflicts with the durable run.");
    expect(screen.getByRole("heading", { name: "Answer needed" }).isConnected).toBe(true);
    expect(screen.getByLabelText("Integer answer").isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(await journal.entries()).toEqual([]);
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
      new Response(JSON.stringify(waitingInputRun()), {
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

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    getRun: vi.fn(async () => waitingInputRun()),
    getWorkflowRevision: vi.fn(async () => revision()),
    ...overrides
  });
}
