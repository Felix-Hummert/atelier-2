import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  encodePublicRunReference,
  type AnyRun,
  type CockpitApi,
  type RunV3,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { MutationJournal, waitMutationId, v3WaitMutation } from "../../src/lib/mutationJournal";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { studioQuestions } from "../../src/lib/studioQuestions";
import { cockpitApiStub } from "../support/cockpitApi";
import { cancellableBlock } from "../support/runV3";

const revisionHash = "a".repeat(64);
const publicReference = encodePublicRunReference("v3/decide");

function waitingRun(overrides: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/decide",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "WAITING_INPUT",
    current_node_id: "approve",
    node_rail: [{ node_id: "approve", state: "needs_you", attempt: null }],
    // A resting Wait is operator-cancellable (#668).
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...overrides
  };
}

function answeredRun(overrides: Partial<RunV3> = {}): RunV3 {
  return waitingRun({
    state: "COMPLETED",
    terminal_hash: "d".repeat(64),
    node_rail: [{ node_id: "approve", state: "succeeded", attempt: null }],
    ended_at: new Date().toISOString(),
    ...overrides
  });
}

function revision(
  kind: "boolean" | "enum" | "free",
  values: string[] | null = null
): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: [],
      orders: [],
      wait_answer_schemas: [
        { node_id: "approve", schema: { ref: "decision", revision: "e".repeat(64) }, kind, values }
      ],
      node_previews: [
        { id: "approve", kind: "wait", role: null, instruction_start: null, depends_on: [] }
      ],
      loops: [],
      name: "Approve once",
      description: null
    }
  } as WorkflowRevisionDetail;
}

function openBoard(runs: readonly AnyRun[], overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", "/atelier");
  const journal = new MutationJournal(sessionStorage);
  const view = render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: vi.fn(async (_after?: string, state?: string) => ({
          items: state === undefined ? [...runs] : runs.filter((run) => run.state === state),
          next_after: null
        })),
        getWorkflowRevision: vi.fn(async () => revision("boolean")),
        ...overrides
      }),
      mutationJournal: journal
    }
  });
  return { ...view, journal };
}

async function expandCard(): Promise<HTMLElement> {
  const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
  await fireEvent.click(within(needsYou).getByRole("button", { name: /Answer here/ }));
  return needsYou;
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => cleanup());

describe("a boolean or enum wait gate answers inline on its Board card (#572)", () => {
  it("costs no graph read until the operator opens the card", async () => {
    const getWorkflowRevision = vi.fn(async () => revision("boolean"));
    openBoard([waitingRun()], { getWorkflowRevision });

    await screen.findByRole("region", { name: "Needs you · 1" });
    expect(getWorkflowRevision).not.toHaveBeenCalled();

    await expandCard();

    await waitFor(() => expect(getWorkflowRevision).toHaveBeenCalledWith(revisionHash));
  });

  it("sends the exact boolean decision through the run page's own audited POST, and the row's own move confirms it", async () => {
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 200, value: answeredRun() };
    });
    openBoard([waitingRun()], { answer, getWorkflowRevision: vi.fn(async () => revision("boolean")) });

    const needsYou = await expandCard();
    expect(within(needsYou).queryByRole("textbox")).toBeNull();
    await fireEvent.click(await within(needsYou).findByRole("button", { name: runPageCopy.answerYes }));

    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
    const body = JSON.parse(
      globalThis.atob((answer.mock.calls[0]?.[0] as { body_base64: string }).body_base64)
    );
    expect(body).toEqual({
      workflow_revision_hash: revisionHash,
      node_id: "approve",
      answer_base64: btoa("true")
    });

    // The row leaving Needs you and disappearing from the Board is the
    // visible confirmation (operator ruling #667) -- a run that turned
    // terminal moves to History, not into a "Done" group the Board still
    // owns, so no separate banner is needed to word it.
    await waitFor(() => expect(screen.queryByRole("region", { name: /Needs you/ })).toBeNull());
    expect(screen.queryByRole("region", { name: /Running/ })).toBeNull();
  });

  it("renders one button per enum member and sends its exact JSON", async () => {
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 200, value: answeredRun() };
    });
    openBoard([waitingRun()], {
      answer,
      getWorkflowRevision: vi.fn(async () => revision("enum", ['"approve"', '"revise"']))
    });

    const needsYou = await expandCard();
    await fireEvent.click(await within(needsYou).findByRole("button", { name: "revise" }));

    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
    const body = JSON.parse(
      globalThis.atob((answer.mock.calls[0]?.[0] as { body_base64: string }).body_base64)
    );
    expect(body.answer_base64).toBe(btoa('"revise"'));
  });

  it("shows the card sending while the request is in flight", async () => {
    let resolveAnswer: (result: { status: 200; value: RunV3 }) => void = () => {};
    const answer = vi.fn(
      () =>
        new Promise<{ status: 200; value: RunV3 }>((resolve) => {
          resolveAnswer = resolve;
        })
    );
    openBoard([waitingRun()], { answer, getWorkflowRevision: vi.fn(async () => revision("boolean")) });

    const needsYou = await expandCard();
    await fireEvent.click(await within(needsYou).findByRole("button", { name: runPageCopy.answerYes }));

    await within(needsYou).findByText("Sending answer");
    // The decision buttons are gone while the request is in flight, not
    // merely disabled: nothing on the card invites a second click at the
    // exact answer already on its way (mirrors the run page's own card).
    expect(within(needsYou).queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();

    resolveAnswer({ status: 200, value: answeredRun() });
    await waitFor(() => expect(screen.queryByRole("region", { name: /Needs you/ })).toBeNull());
  });

  it("keeps a lost answer visible with Retry, and Retry re-sends the identical exact bytes", async () => {
    const answer = vi
      .fn()
      .mockRejectedValueOnce(new CockpitRequestError("The connection ended without a response."))
      .mockResolvedValueOnce({ status: 200, value: answeredRun() });
    openBoard([waitingRun()], { answer, getWorkflowRevision: vi.fn(async () => revision("boolean")) });

    const needsYou = await expandCard();
    await fireEvent.click(await within(needsYou).findByRole("button", { name: runPageCopy.answerYes }));

    await within(needsYou).findByRole("alert", { name: "Send uncertain" });
    await within(needsYou).findByText("Answer uncertain");
    expect(answer).toHaveBeenCalledTimes(1);
    const firstRequest = answer.mock.calls[0]?.[0] as { body_base64: string };

    await fireEvent.click(within(needsYou).getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
    expect(answer.mock.calls[1]?.[0]).toMatchObject({ body_base64: firstRequest.body_base64 });
    await waitFor(() => expect(screen.queryByRole("region", { name: /Needs you/ })).toBeNull());
  });

  it("lets the operator discard a lost answer and try again", async () => {
    const answer = vi.fn().mockRejectedValue(new CockpitRequestError("offline"));
    const { journal } = openBoard([waitingRun()], {
      answer,
      getWorkflowRevision: vi.fn(async () => revision("boolean"))
    });

    const needsYou = await expandCard();
    await fireEvent.click(await within(needsYou).findByRole("button", { name: runPageCopy.answerYes }));
    await within(needsYou).findByRole("button", { name: "Discard" });

    await fireEvent.click(within(needsYou).getByRole("button", { name: "Discard" }));

    await waitFor(async () =>
      expect(await journal.get(waitMutationId(publicReference, "approve"))).toBeNull()
    );
    expect(within(needsYou).getByRole("button", { name: runPageCopy.answerYes }).isConnected).toBe(true);
  });

  it("shows the canonical run instead of a stale decidable card when the answer conflicts with a parallel one on the run page", async () => {
    const conflict = new CockpitRequestError(
      "The durable run is no longer waiting for this answer.",
      {
        type: "urn:atelier2:problem:v1:answer-state-conflict",
        title: "Answer state conflict",
        status: 409,
        detail: "The durable run is no longer waiting for this answer."
      },
      true
    );
    const answer = vi.fn().mockRejectedValue(conflict);
    const getRun = vi.fn(async () => answeredRun({ state_version: 2 }));
    openBoard([waitingRun()], {
      answer,
      getRun,
      getWorkflowRevision: vi.fn(async () => revision("boolean"))
    });

    const needsYou = await expandCard();
    await fireEvent.click(await within(needsYou).findByRole("button", { name: runPageCopy.answerYes }));

    await waitFor(() => expect(getRun).toHaveBeenCalledWith(publicReference));
    // The card never keeps offering an answer the durable run already moved
    // past -- it shows what is true now instead (operator ruling, #572), and
    // a run resolved to terminal leaves the Board for History (#667).
    await waitFor(() => expect(screen.queryByRole("region", { name: /Needs you/ })).toBeNull());
    expect(screen.queryByRole("region", { name: /Running/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("restores a decision left uncertain by an earlier attempt instead of offering the node twice", async () => {
    const journal = new MutationJournal(sessionStorage);
    const mutation = await v3WaitMutation(publicReference, revisionHash, "approve", "true");
    await journal.prepare(mutation);
    await journal.markUncertain(mutation.mutation_id);
    window.history.replaceState(null, "", "/atelier");

    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          listRuns: vi.fn(async (_after?: string, state?: string) => ({
            items: state === undefined || state === "WAITING_INPUT" ? [waitingRun()] : [],
            next_after: null
          })),
          getWorkflowRevision: vi.fn(async () => revision("boolean"))
        }),
        mutationJournal: journal
      }
    });

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    expect(await within(needsYou).findByText("Answer uncertain")).toBeTruthy();
    expect(
      await within(needsYou).findByText(`${runPageCopy.answeredPrefix} ${runPageCopy.answerYes}`)
    ).toBeTruthy();
  });

  it("opens on its own and names a journaled answer that no longer matches this waiting node, instead of staying silently collapsed", async () => {
    // The realistic shape of "corrupt": an earlier attempt journaled an
    // answer for this same node under a workflow revision the run has since
    // moved past (e.g. republished between attempts).
    const journal = new MutationJournal(sessionStorage);
    const staleRevisionHash = "f".repeat(64);
    const mutation = await v3WaitMutation(publicReference, staleRevisionHash, "approve", "true");
    await journal.prepare(mutation);
    window.history.replaceState(null, "", "/atelier");

    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          listRuns: vi.fn(async (_after?: string, state?: string) => ({
            items: state === undefined || state === "WAITING_INPUT" ? [waitingRun()] : [],
            next_after: null
          })),
          getWorkflowRevision: vi.fn(async () => revision("boolean"))
        }),
        mutationJournal: journal
      }
    });

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    // Opens without a click: a person who left mid decision must see the
    // problem, not find the card collapsed again behind "Answer here".
    const alert = await within(needsYou).findByRole("alert", { name: "Send failed" });
    expect(alert.textContent).toContain("The saved exact answer does not belong to this waiting node.");
    // Never a guess at decision buttons for an identity this surface cannot
    // trust -- the honest fallback and its link to the run page instead.
    expect(within(needsYou).queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
    expect(within(needsYou).getByText("This needs a written answer.")).toBeTruthy();
    expect(within(needsYou).getByRole("link", { name: "Open the run to answer" }).isConnected).toBe(true);
  });

  it("names a free-text gate honestly and leads to the run page instead of offering buttons it cannot answer here", async () => {
    openBoard([waitingRun()], { getWorkflowRevision: vi.fn(async () => revision("free")) });

    const needsYou = await expandCard();

    await within(needsYou).findByText("This needs a written answer.");
    expect(within(needsYou).queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
    const open = within(needsYou).getByRole("link", { name: "Open the run to answer" });

    await fireEvent.click(open);

    await waitFor(() => expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`));
  });

  it("every inline control on the card answers a named Board question", async () => {
    openBoard([waitingRun()], { getWorkflowRevision: vi.fn(async () => revision("boolean")) });

    const needsYou = await expandCard();
    const toggle = within(needsYou).getByRole("button", { name: /Answer here/ });
    const yes = await within(needsYou).findByRole("button", { name: runPageCopy.answerYes });

    expect(toggle.getAttribute("data-studio-question")).toBe(studioQuestions.answerHere.id);
    expect(yes.getAttribute("data-studio-question")).toBe(studioQuestions.answerDecision.id);
  });
});
