import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  encodePublicRunReference,
  type AnyRun,
  type CockpitApi,
  type Problem,
  type RunV1,
  type RunV3
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { standingMarks } from "../../src/lib/runState";
import {
  describeStudioControl,
  questionForStudioControl,
  studioInteractiveSelector,
  studioQuestions,
  studioStageSelector,
  unansweredStudioControls
} from "../../src/lib/studioQuestions";
import { cockpitApiStub, FakeRunEventFeed, PAGE_CURSORS } from "../support/cockpitApi";
import {
  completedRun,
  eventCursor,
  publicReference,
  revisionHash,
  startedRun,
  waitingInput,
  waitingInputRun,
  waitingReconciliationRun
} from "../support/workflowV1";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

function listRunsByState(runs: AnyRun[]) {
  return vi.fn(async (_after?: string, state?: string) => ({
    items: state === undefined ? runs : runs.filter((run) => run.state === state),
    next_after: null
  }));
}

function openStudio(runs: AnyRun[] = [], overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", "/atelier");
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: listRunsByState(runs),
        ...overrides
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

function openStudioHolding(runs: AnyRun[] = [], overrides: Partial<CockpitApi> = {}) {
  const feed = new FakeRunEventFeed();
  const view = openStudio(runs, { openAttentionEvents: feed.openAttention, ...overrides });
  return { feed, ...view };
}

function expectStudioControlsAnswerNamedQuestions(
  expected: ReadonlyArray<(typeof studioQuestions)[keyof typeof studioQuestions]["id"]>
): void {
  const stage = document.querySelector(studioStageSelector);
  if (stage === null) {
    throw new Error("Studio stage is missing");
  }
  const unanswered = unansweredStudioControls(stage);
  expect(
    unanswered.map(describeStudioControl),
    unanswered.map(describeStudioControl).join("; ")
  ).toEqual([]);
  const present = [...stage.querySelectorAll(studioInteractiveSelector)].map((element) => {
    const found = questionForStudioControl(element);
    if (found === null) {
      throw new Error(`unmapped Studio control: ${describeStudioControl(element)}`);
    }
    return found.id;
  });
  expect(new Set(present)).toEqual(new Set(expected));
}

function listedV3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/two-agents",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [{ node_id: "review", state: "working", attempt: null }],
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...changes
  };
}

function failedRun(changes: Partial<RunV1> = {}): RunV1 {
  return startedRun({
    state: "FAILED",
    terminal_hash: revisionHash,
    ...changes
  });
}

function agentFailedEvent() {
  return {
    workflow_format_version: 2 as const,
    cursor: eventCursor(1),
    sequence: 1,
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    node_id: "agent",
    node_execution_id: revisionHash,
    event_hash: revisionHash,
    node_rail: [
      {
        node_id: "agent",
        state: "failed" as const,
        attempt: { ordinal: 1 as const, state: "FAILED" as const }
      }
    ],
    event: "AGENT_FAILED" as const,
    failure_code: "PROCESS_EXITED_UNSUCCESSFULLY" as const,
    attempt_id: revisionHash,
    attempt_ordinal: 1 as const
  };
}

function streamFailedFrame() {
  return {
    event: "STREAM_FAILED",
    problem: {
      type: "urn:atelier2:problem:v1:durable-state-corrupt",
      title: "Durable state is corrupt",
      status: 500,
      detail: "Stop mutation and inspect the durable store."
    }
  };
}

describe("the studio is the level the workshop opens on", () => {
  it("proves(the-workshop-opens-in-the-studio): opens the bare atelier path in the Studio instead of a list of runs", async () => {
    openStudio();

    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Runs" })).toBeNull();
    expect(window.location.pathname).toBe("/atelier");
  });

  it("asks the durable list by the states the card and inbox can name", async () => {
    const listRuns = listRunsByState([startedRun()]);
    openStudio([startedRun()], { listRuns });
    await screen.findByRole("article", { name: "This workshop" });

    expect(listRuns.mock.calls.map(([, state]) => state).sort()).toEqual([
      "COMPLETED",
      "FAILED",
      "STARTED",
      "WAITING_INPUT",
      "WAITING_RECONCILIATION"
    ]);
  });

  it("carries one project card for this installation, with counts it can read", async () => {
    openStudio([
      startedRun({ public_run_reference: "run1.YQ" }),
      startedRun({ public_run_reference: "run1.Yg" }),
      waitingInputRun({ public_run_reference: "run1.Yw" }),
      failedRun({ public_run_reference: "run1.ZQ" }),
      completedRun({ public_run_reference: "run1.ZA" })
    ]);

    const card = await screen.findByRole("article", { name: "This workshop" });

    expect(within(card).getByText("2 running").isConnected).toBe(true);
    expect(within(card).getByText("1 waiting for you").isConnected).toBe(true);
    expect(within(card).getByText("1 failed").isConnected).toBe(true);
    expect(within(card).getByText(standingMarks.failed).isConnected).toBe(true);
    expect(within(card).getByText("1 landed").isConnected).toBe(true);
    expect(within(card).getAllByRole("link")).toHaveLength(1);
  });

  it("leads from the one project card down into the project level", async () => {
    openStudio([startedRun()]);
    const card = await screen.findByRole("article", { name: "This workshop" });

    await fireEvent.click(within(card).getByRole("link"));

    expect(window.location.pathname).toBe("/atelier/project");
    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Studio" })).toBeNull();
  });

  it("keeps real workshop work ahead of an unavailable chat", async () => {
    openStudio([waitingInputRun(), startedRun({ public_run_reference: "run1.YQ" })]);

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });
    const card = await screen.findByRole("article", { name: "This workshop" });
    const chat = await screen.findByRole("region", { name: "Chat" });

    expect(inbox.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(within(chat).getByText("Unavailable").isConnected).toBe(true);
    expect(card.compareDocumentPosition(chat) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });
});

describe("the inbox names what waits for a human", () => {
  it("proves(the-inbox-names-every-run-that-waits-for-a-human): names every run in a durable waiting state and no run that waits for nobody, across every page the list holds", async () => {
    // "Across everything" is only true while the reading spans the durable
    // pages: a run that waits on the second page is exactly the one an inbox
    // stopping at the first would lose.
    window.history.replaceState(null, "", "/atelier");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          listRuns: vi.fn(async (after?: string, state?: string) => {
            if (state === "WAITING_INPUT") {
              return after === undefined
                ? {
                    items: [waitingInputRun({ public_run_reference: "run1.Yg" })],
                    next_after: PAGE_CURSORS[0] ?? null
                  }
                : {
                    items: [waitingInputRun({ public_run_reference: "run1.YQ" })],
                    next_after: null
                  };
            }
            if (state === "WAITING_RECONCILIATION") {
              return {
                items: [waitingReconciliationRun({ public_run_reference: "run1.Yw" })],
                next_after: null
              };
            }
            return { items: [], next_after: null };
          })
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });

    const waiting = within(inbox).getAllByRole("link");
    expect(waiting).toHaveLength(3);
    expect(within(inbox).getAllByText("Answer")).toHaveLength(2);
    expect(within(inbox).getByText("Reconcile").isConnected).toBe(true);
  });

  it("stays silent when nothing waits for a human", async () => {
    openStudio([startedRun(), completedRun({ public_run_reference: "run1.Yg" })]);

    await screen.findByRole("article", { name: "This workshop" });

    expect(screen.queryByRole("region", { name: "Waiting for you" })).toBeNull();
  });

  it("opens the waiting run with one click", async () => {
    openStudio([waitingInputRun()], { getRun: vi.fn(async () => waitingInputRun()) });

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });
    await fireEvent.click(within(inbox).getByRole("link", { name: /Answer/ }));

    await waitFor(() => expect(window.location.pathname).toBe("/atelier/runs/run1.cnVu"));
  });
});

describe("an empty studio teaches the one next action", () => {
  it("proves(an-empty-area-names-the-one-next-action): names starting a run as the one action possible today, and offers it once", async () => {
    const { feed } = openStudioHolding([]);
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();

    const empty = await screen.findByRole("heading", { name: "Nothing is running" });

    expect(empty.isConnected).toBe(true);
    expect(screen.getByText("Live").isConnected).toBe(true);
    expect(screen.queryByText("Connecting")).toBeNull();
    expect(screen.getAllByRole("link", { name: "Start a run" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("link", { name: "Start a run" }));

    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
  });

  it("tells the truth while it is still looking, and names the failed read without raw transport text", async () => {
    const listRuns = vi.fn(() => new Promise<never>(() => undefined));
    openStudio([], { listRuns } as Partial<CockpitApi>);

    expect((await screen.findByText("Looking…")).isConnected).toBe(true);

    cleanup();
    const problem = {
      type: "urn:atelier2:problem:v1:temporarily-unavailable",
      title: "Temporarily unavailable",
      status: 503,
      detail: "private adapter failure"
    } as Problem;
    openStudio([], {
      listRuns: vi.fn().mockRejectedValue(
        new CockpitRequestError("raw transport failure", problem)
      )
    });

    expect((await screen.findByText("Studio runs unavailable")).isConnected).toBe(true);
    expect(screen.queryByText(/raw transport failure|private adapter failure|Failed to fetch/))
      .toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry studio runs" })).toHaveLength(1);
  });

  it("repeats only the failed Studio read until a successful retry replaces the error", async () => {
    const listRuns = vi.fn(async (_after?: string, state?: string) => {
      const round = Math.floor((listRuns.mock.calls.length - 1) / 5);
      if (round < 2) throw new Error("socket detail must stay private");
      return {
        items: state === "STARTED" ? [startedRun()] : [],
        next_after: null
      };
    });
    openStudio([], { listRuns });

    await screen.findByText("Studio runs unavailable");
    const retry = screen.getByRole("button", { name: "Retry studio runs" });
    await fireEvent.click(retry);
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(10));
    await screen.findByRole("button", { name: "Retry studio runs" });
    expect(screen.getAllByRole("button", { name: "Retry studio runs" })).toHaveLength(1);
    expect(screen.queryByText(/socket detail/)).toBeNull();

    await fireEvent.click(retry);

    expect((await screen.findByRole("article", { name: "This workshop" })).isConnected).toBe(true);
    expect(listRuns).toHaveBeenCalledTimes(15);
    expect(screen.queryByRole("button", { name: "Retry studio runs" })).toBeNull();
    expect(screen.getByRole("button", { name: "Refresh studio runs" }).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });

  it("keeps confirmed Studio truth through a failed refresh and confirms newer truth after Retry", async () => {
    let response: "started" | "failed" | "completed" = "started";
    const listRuns = vi.fn(async (_after?: string, state?: string) => {
      if (response === "failed") throw new Error("wire detail");
      const run = response === "started" ? startedRun() : completedRun();
      return { items: state === run.state ? [run] : [], next_after: null };
    });
    openStudio([], { listRuns });
    const card = await screen.findByRole("article", { name: "This workshop" });
    expect(within(card).getByText("1 running").isConnected).toBe(true);

    response = "failed";
    await fireEvent.click(screen.getByRole("button", { name: "Refresh studio runs" }));

    await screen.findByText("Studio runs unavailable");
    expect(within(card).getByText("1 running").isConnected).toBe(true);
    response = "completed";
    await fireEvent.click(screen.getByRole("button", { name: "Retry studio runs" }));

    await waitFor(() => expect(within(card).getByText("1 landed").isConnected).toBe(true));
    expect(within(card).queryByText("1 running")).toBeNull();
  });

  it("does not confirm a partial initial five-list reading", async () => {
    const listRuns = vi.fn(async (after?: string, state?: string) => {
      if (state === "STARTED" && after === undefined) {
        return { items: [startedRun()], next_after: "run1.bmV4dA" };
      }
      if (state === "STARTED") throw new Error("later page detail");
      return { items: [], next_after: null };
    });
    openStudio([], { listRuns });

    expect((await screen.findByText("Studio runs incomplete")).isConnected).toBe(true);
    expect(screen.queryByRole("article", { name: "This workshop" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Nothing is running" })).toBeNull();
    expect(screen.queryByText(/later page detail/)).toBeNull();
  });
});

describe("the studio holds GET /events", () => {
  it("holds the attention stream when the studio opens", async () => {
    const { feed } = openStudioHolding([]);

    await screen.findByRole("heading", { name: "Studio" });

    expect(feed.openAttention).toHaveBeenCalledWith(expect.any(Object));
  });

  it("names connecting as itself, not as an empty workshop", async () => {
    openStudioHolding([]);

    expect((await screen.findByText("Connecting")).isConnected).toBe(true);
    await waitFor(() => expect(screen.queryByText("Looking…")).toBeNull());
    expect(screen.queryByRole("heading", { name: "Nothing is running" })).toBeNull();
  });

  it("applies a WAITING_INPUT from the stream without already listing the run", async () => {
    const waiting = waitingInputRun({ public_run_reference: "run1.YQ" });
    const getRun = vi.fn(async () => waiting);
    const { feed } = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: "Nothing is running" });

    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(inbox).getByRole("link", { name: /Answer/ }).isConnected).toBe(true);
    expect(getRun).toHaveBeenCalledWith("run1.YQ");
    expect(screen.queryByRole("heading", { name: "Nothing is running" })).toBeNull();
    expect(within(await screen.findByRole("article", { name: "This workshop" })).getByText("1 waiting for you").isConnected).toBe(true);
  });

  it("applies an AGENT_FAILED from the stream without already listing the run", async () => {
    const getRun = vi.fn(async () => failedRun());
    const { feed } = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: "Nothing is running" });

    feed.handlers?.event(JSON.stringify(agentFailedEvent()));

    const card = await screen.findByRole("article", { name: "This workshop" });
    expect(within(card).getByText("1 failed").isConnected).toBe(true);
    expect(within(card).getByText(standingMarks.failed).isConnected).toBe(true);
    expect(within(card).queryByText("1 landed")).toBeNull();
    expect(getRun).toHaveBeenCalledWith(publicReference);
    expect(screen.queryByRole("region", { name: "Waiting for you" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Nothing is running" })).toBeNull();
  });

  it("keeps a projected wait when a slower list later answers the same run as started", async () => {
    let releaseStarted: (page: { items: RunV1[]; next_after: null }) => void = () => {
      throw new Error("STARTED list was released before the test held it");
    };
    const startedPage = new Promise<{ items: RunV1[]; next_after: null }>((resolve) => {
      releaseStarted = resolve;
    });
    const waiting = waitingInputRun({ public_run_reference: "run1.YQ" });
    const staleStarted = startedRun({ public_run_reference: "run1.YQ" });
    const otherStarted = startedRun({ public_run_reference: "run1.Yg", run_id: "other" });
    const listRuns = vi.fn(async (_after?: string, state?: string) => {
      if (state === "STARTED") return startedPage;
      return { items: [], next_after: null };
    });
    const getRun = vi.fn(async () => waiting);
    const { feed } = openStudioHolding([], { listRuns, getRun });
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();

    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(inbox).getByRole("link", { name: /Answer/ }).isConnected).toBe(true);

    releaseStarted({ items: [staleStarted, otherStarted], next_after: null });
    const card = await screen.findByRole("article", { name: "This workshop" });
    await waitFor(() => {
      expect(within(card).getByText("1 running").isConnected).toBe(true);
    });
    expect(within(card).getByText("1 waiting for you").isConnected).toBe(true);
    expect(within(card).queryByText("2 running")).toBeNull();
    expect(within(screen.getByRole("region", { name: "Waiting for you" })).getByRole("link", { name: /Answer/ }).isConnected).toBe(true);
  });

  it("replaces a projected wait when a later list answers the same run as completed at a newer version", async () => {
    let releaseCompleted: (page: { items: RunV1[]; next_after: null }) => void = () => {
      throw new Error("COMPLETED list was released before the test held it");
    };
    const completedPage = new Promise<{ items: RunV1[]; next_after: null }>((resolve) => {
      releaseCompleted = resolve;
    });
    const waiting = waitingInputRun({ public_run_reference: "run1.YQ" });
    const newerCompleted = completedRun({ public_run_reference: "run1.YQ", state_version: 4 });
    const listRuns = vi.fn(async (_after?: string, state?: string) => {
      if (state === "COMPLETED") return completedPage;
      return { items: [], next_after: null };
    });
    const getRun = vi.fn(async () => waiting);
    const { feed } = openStudioHolding([], { listRuns, getRun });
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();

    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(inbox).getByRole("link", { name: /Answer/ }).isConnected).toBe(true);

    releaseCompleted({ items: [newerCompleted], next_after: null });
    const card = await screen.findByRole("article", { name: "This workshop" });
    await waitFor(() => {
      expect(within(card).getByText("1 landed").isConnected).toBe(true);
    });
    expect(within(card).queryByText("1 waiting for you")).toBeNull();
    expect(screen.queryByRole("region", { name: "Waiting for you" })).toBeNull();
  });

  it("retries a failed getRun until the delivered wait is visible once", async () => {
    const waiting = waitingInputRun({ public_run_reference: "run1.YQ" });
    const getRun = vi
      .fn()
      .mockRejectedValueOnce(new Error("run missing"))
      .mockResolvedValueOnce(waiting);
    const { feed } = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: "Nothing is running" });

    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );

    expect((await screen.findByText("run missing")).isConnected).toBe(true);
    expect(screen.getByText("Live").isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: "Waiting for you" })).toBeNull();
    expect(screen.queryAllByRole("link", { name: /Start/ })).toHaveLength(0);
    expect(getRun).toHaveBeenCalledTimes(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(inbox).getAllByRole("link", { name: /Answer/ })).toHaveLength(1);
    expect(getRun).toHaveBeenCalledTimes(2);
    expect(getRun).toHaveBeenNthCalledWith(1, "run1.YQ");
    expect(getRun).toHaveBeenNthCalledWith(2, "run1.YQ");
    expect(screen.queryByText("run missing")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("stops the stream on STREAM_FAILED even while a projection is waiting for Retry", async () => {
    const waiting = waitingInputRun({ public_run_reference: "run1.YQ" });
    const getRun = vi
      .fn()
      .mockRejectedValueOnce(new Error("run missing"))
      .mockResolvedValueOnce(waiting);
    const { feed } = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: "Nothing is running" });

    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );
    expect((await screen.findByText("run missing")).isConnected).toBe(true);

    feed.handlers?.event(JSON.stringify(streamFailedFrame()));

    expect((await screen.findByText("Stopped")).isConnected).toBe(true);
    expect(screen.getByText("Durable state is corrupt").isConnected).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" }).isConnected).toBe(true);
    expect(screen.queryAllByRole("link", { name: /Start/ })).toHaveLength(0);
    expect(screen.queryByText("Live")).toBeNull();
    expect(feed.close).toHaveBeenCalled();

    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(inbox).getAllByRole("link", { name: /Answer/ })).toHaveLength(1);
    expect(screen.getByText("Stopped").isConnected).toBe(true);
    expect(screen.queryAllByRole("link", { name: /Start/ })).toHaveLength(0);
  });

  it("names a failed attention stream as itself, not as an empty workshop", async () => {
    const { feed } = openStudioHolding([]);
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: "Nothing is running" });

    feed.handlers?.event(JSON.stringify(streamFailedFrame()));

    expect((await screen.findByText("Stopped")).isConnected).toBe(true);
    expect(screen.getByText("Durable state is corrupt").isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Nothing is running" })).toBeNull();
    expect(screen.queryByText("Connecting")).toBeNull();
    expect(screen.queryByText("Live")).toBeNull();
    expect(screen.queryAllByRole("link", { name: /Start/ })).toHaveLength(0);
    expect(feed.close).toHaveBeenCalled();
  });
});

describe("every Studio control answers a named user question", () => {
  it("proves(studio-elements-answer-named-questions): every interactive Studio control is listed against one named user question", async () => {
    const ids = Object.values(studioQuestions).map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const entry of Object.values(studioQuestions)) {
      expect(entry.question.endsWith("?")).toBe(true);
    }

    openStudio([
      startedRun({ public_run_reference: "run1.YQ" }),
      waitingInputRun({ public_run_reference: "run1.Yw" }),
      failedRun({ public_run_reference: "run1.ZQ" }),
      completedRun({ public_run_reference: "run1.ZA" }),
      listedV3Run({
        run_id: "done-v3",
        public_run_reference: encodePublicRunReference("done-v3"),
        state: "COMPLETED",
        terminal_hash: revisionHash,
        node_rail: [{ node_id: "review", state: "succeeded", attempt: null }],
        ended_at: "2026-08-18T12:00:00Z"
      })
    ]);
    await screen.findByRole("article", { name: "This workshop" });
    await screen.findByRole("button", { name: "Exact time" });
    expectStudioControlsAnswerNamedQuestions([
      studioQuestions.start.id,
      studioQuestions.inboxRun.id,
      studioQuestions.project.id,
      studioQuestions.whyOneProject.id,
      studioQuestions.lastLandingTime.id,
      studioQuestions.reloadStudioRuns.id
    ]);

    cleanup();
    const { feed } = openStudioHolding([]);
    await screen.findByRole("heading", { name: "Studio" });
    feed.handlers?.opened();
    await screen.findByRole("link", { name: "Start a run" });
    expectStudioControlsAnswerNamedQuestions([
      studioQuestions.emptyStart.id,
      studioQuestions.reloadStudioRuns.id
    ]);

    cleanup();
    openStudio([], {
      listRuns: vi.fn().mockRejectedValue(new Error("wire detail"))
    });
    await screen.findByRole("button", { name: "Retry studio runs" });
    expectStudioControlsAnswerNamedQuestions([studioQuestions.reloadStudioRuns.id]);

    cleanup();
    const getRun = vi.fn().mockRejectedValueOnce(new Error("run missing"));
    const projection = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Studio" });
    projection.feed.handlers?.opened();
    await screen.findByRole("heading", { name: "Nothing is running" });
    projection.feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );
    expect((await screen.findByText("run missing")).isConnected).toBe(true);
    expectStudioControlsAnswerNamedQuestions([
      studioQuestions.retryProjection.id,
      studioQuestions.reloadStudioRuns.id
    ]);
  });
});
