import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  encodePublicRunReference,
  type CockpitApi,
  type RunV3,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { cockpitApiStub } from "../support/cockpitApi";
import { cancellableBlock } from "../support/runV3";

/**
 * The Workbench pins every open decision in its own non-scrolling "Needs you"
 * region so a request can never be lost in the growing conversation -- the
 * lived failure mode that ruling names (#580). These tests drive that region
 * through the real surface: it holds a decision while the operator keeps
 * typing, survives leaving and returning, and answers it on the one audited
 * path the run page and the Board already share.
 */

const revisionHash = "a".repeat(64);
const publicReference = encodePublicRunReference("v3/decide");
const question = "Ship it, or hold it back?";

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

function answeredRun(): RunV3 {
  return waitingRun({
    state: "COMPLETED",
    terminal_hash: "d".repeat(64),
    node_rail: [{ node_id: "approve", state: "succeeded", attempt: null }],
    ended_at: new Date().toISOString()
  });
}

function revision(kind: "boolean" | "enum" | "free", values: string[] | null = null): WorkflowRevisionDetail {
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
      node_previews: [{ id: "approve", kind: "wait", role: null, instruction_start: null, depends_on: [] }],
      loops: [],
      name: "Approve once",
      description: null
    }
  } as WorkflowRevisionDetail;
}

function questionDetail(job: string | null = question) {
  return {
    run_id: "v3/decide",
    public_run_reference: publicReference,
    node_id: "approve",
    state: "needs_you",
    job_base64: job === null ? null : btoa(job),
    job_hash: job === null ? null : "e".repeat(64),
    answer: null,
    provenance: null,
    refusal: null
  };
}

function openWorkbench(runs: readonly RunV3[], overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", "/atelier/chat");
  const journal = new MutationJournal(sessionStorage);
  const view = render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: vi.fn(async (_after?: string, state?: string) => ({
          items: state === "WAITING_INPUT" ? [...runs] : [],
          next_after: null
        })),
        getNodeDetail: vi.fn(async () => questionDetail() as never),
        getWorkflowRevision: vi.fn(async () => revision("boolean")),
        ...overrides
      }),
      mutationJournal: journal
    }
  });
  return { ...view, journal };
}

async function say(words: string): Promise<void> {
  await fireEvent.input(screen.getByLabelText(workbenchPageCopy.composerLabel), {
    target: { value: words }
  });
  await fireEvent.click(screen.getByRole("button", { name: workbenchPageCopy.send }));
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => cleanup());

describe("the Workbench pins open decisions (#580)", () => {
  it("proves(the-workbench-pins-an-open-decision-until-it-is-answered): holds the decision through continued chatting and a walk away and back, then retires it once answered on the shared audited path", async () => {
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 200 as const, value: answeredRun() };
    });
    openWorkbench([waitingRun()], { answer });

    const needsYou = await screen.findByRole("region", { name: question });
    expect((await within(needsYou).findByRole("heading", { name: question })).isConnected).toBe(true);
    await within(needsYou).findByRole("button", { name: runPageCopy.answerYes });

    // The whole point: the decision lives in the pinned region, never inside
    // the conversation that scrolls. Three sent turns grow the stream; the
    // decision does not move into it and does not disappear.
    await say("start the preview door");
    await say("and the wait bug");
    await say("in parallel");
    const conversation = screen.getByRole("list", { name: workbenchPageCopy.transcriptLabel });
    expect(within(conversation).queryByRole("heading", { name: question })).toBeNull();
    expect(within(needsYou).getByRole("heading", { name: question }).isConnected).toBe(true);

    // Leaving for another room and coming back is not answering: the pin is
    // read again and stands.
    const rail = screen.getByRole("navigation", { name: "Workshop" });
    await fireEvent.click(within(rail).getByRole("link", { name: "Catalog" }));
    await screen.findByRole("heading", { level: 1, name: "Catalog" });
    await fireEvent.click(
      within(screen.getByRole("navigation", { name: "Workshop" })).getByRole("link", {
        name: new RegExp(workbenchPageCopy.title)
      })
    );
    const returned = await screen.findByRole("region", { name: question });
    await within(returned).findByRole("button", { name: runPageCopy.answerYes });

    // Answering sends the exact JSON the click decides, through the one audited
    // POST -- the same body the run page sends.
    await fireEvent.click(within(returned).getByRole("button", { name: runPageCopy.answerYes }));
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob((answer.mock.calls[0]?.[0] as { body_base64: string }).body_base64));
    expect(body).toEqual({
      workflow_revision_hash: revisionHash,
      node_id: "approve",
      expected_node_execution_id: "d".repeat(64),
      actor: "operator",
      answer_base64: btoa("true")
    });

    // Answered, the pin is retired -- the room shows the question no longer,
    // rather than keeping one the run no longer asks.
    await waitFor(() => expect(screen.queryByRole("heading", { name: question })).toBeNull());
  });

  it("greets an empty workshop instead of pinning a decision that is not there", async () => {
    openWorkbench([]);

    await screen.findByRole("heading", { level: 1, name: workbenchPageCopy.title });
    // Nothing waits, so nothing is said about it: the absence is the shape of
    // the room, not a sentence (ADR 0019 §3).
    expect(screen.queryByRole("heading", { name: question })).toBeNull();
  });

  it("leads a written decision to the run page rather than offering buttons the pin cannot answer here", async () => {
    openWorkbench([waitingRun()], { getWorkflowRevision: vi.fn(async () => revision("free")) });

    const needsYou = await screen.findByRole("region", { name: question });
    await within(needsYou).findByRole("heading", { name: question });
    expect(within(needsYou).queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
    const open = within(needsYou).getByRole("link", { name: workbenchPageCopy.openTheRun });

    await fireEvent.click(open);

    await waitFor(() => expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`));
  });

  it("keeps six decisions bounded to one expanded stage and promotes the compact control", async () => {
    const runs = Array.from({ length: 6 }, (_, index) =>
      waitingRun({
        run_id: `v3/decide-${index + 1}`,
        public_run_reference: encodePublicRunReference(`v3/decide-${index + 1}`)
      })
    );
    openWorkbench(runs);

    const decisions = await screen.findAllByRole("region", { name: question });
    expect(decisions).toHaveLength(6);
    const expanded = decisions[0];
    const compact = decisions[5];
    if (expanded === undefined || compact === undefined) {
      throw new Error("The decision stack did not render.");
    }

    expect(within(expanded).getByRole("link", { name: workbenchPageCopy.openTheRun }).isConnected).toBe(true);
    expect(
      within(compact).getByRole("button", {
        name: new RegExp(workbenchPageCopy.answerDecision)
      }).isConnected
    ).toBe(true);
    expect(
      within(compact).queryByRole("link", {
        name: workbenchPageCopy.openTheRun
      })
    ).toBeNull();

    await fireEvent.click(
      within(compact).getByRole("button", {
        name: new RegExp(workbenchPageCopy.answerDecision)
      })
    );

    await waitFor(() => {
      expect(
        within(compact).getByRole("link", {
          name: workbenchPageCopy.openTheRun
        }).isConnected
      ).toBe(true);
      expect(
        within(expanded).queryByRole("link", {
          name: workbenchPageCopy.openTheRun
        })
      ).toBeNull();
      expect(
        within(expanded).getByRole("button", {
          name: new RegExp(workbenchPageCopy.answerDecision)
        }).isConnected
      ).toBe(true);
    });
  });

  it("keeps an uncertain answer expanded when another decision is promoted", async () => {
    const answer = vi.fn(async () => {
      throw new CockpitRequestError("The connection ended without a response.");
    });
    openWorkbench(
      [
        waitingRun(),
        waitingRun({
          run_id: "v3/decide-second",
          public_run_reference: encodePublicRunReference("v3/decide-second")
        })
      ],
      { answer }
    );

    const decisions = await screen.findAllByRole("region", { name: question });
    const answeredDecision = decisions[0];
    const otherDecision = decisions[1];
    if (answeredDecision === undefined || otherDecision === undefined) {
      throw new Error("The decision stack did not render.");
    }

    await fireEvent.click(
      within(answeredDecision).getByRole("button", {
        name: runPageCopy.answerYes
      })
    );
    await within(answeredDecision).findByRole("heading", {
      name: "Answer uncertain"
    });
    await within(answeredDecision).findByRole("button", { name: runPageCopy.retry });

    await fireEvent.click(
      within(otherDecision).getByRole("button", {
        name: new RegExp(workbenchPageCopy.answerDecision)
      })
    );

    await within(otherDecision).findByRole("link", {
      name: workbenchPageCopy.openTheRun
    });
    expect(
      within(answeredDecision).getByRole("heading", {
        name: "Answer uncertain"
      }).isConnected
    ).toBe(true);
    expect(within(answeredDecision).getByRole("button", { name: runPageCopy.retry }).isConnected).toBe(true);
    expect(
      within(answeredDecision).queryByRole("button", {
        name: new RegExp(workbenchPageCopy.answerDecision)
      })
    ).toBeNull();
  });
});
