import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV1 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { standingMarks } from "../../src/lib/runState";
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

function listRunsByState(runs: RunV1[]) {
  return vi.fn(async (_after?: string, state?: string) => ({
    items: state === undefined ? runs : runs.filter((run) => run.state === state),
    next_after: null
  }));
}

function openStudio(runs: RunV1[] = [], overrides: Partial<CockpitApi> = {}) {
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

function openStudioHolding(runs: RunV1[] = [], overrides: Partial<CockpitApi> = {}) {
  const feed = new FakeRunEventFeed();
  const view = openStudio(runs, { openAttentionEvents: feed.openAttention, ...overrides });
  return { feed, ...view };
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

  it("tells the truth while it is still looking, and when the read fails", async () => {
    const listRuns = vi.fn(() => new Promise<never>(() => undefined));
    openStudio([], { listRuns } as Partial<CockpitApi>);

    expect((await screen.findByText("Looking…")).isConnected).toBe(true);

    cleanup();
    openStudio([], { listRuns: vi.fn().mockRejectedValue(new Error("offline")) });

    expect((await screen.findByText(/offline/)).isConnected).toBe(true);
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

describe("the chat is a named door, not a dead field", () => {
  it("names the conductor as missing instead of offering an input that answers nobody", async () => {
    openStudio([startedRun()]);

    const chat = await screen.findByRole("region", { name: "Chat" });

    expect(within(chat).getByText(/not built yet/).isConnected).toBe(true);
    expect(within(chat).queryByRole("textbox")).toBeNull();
    expect(within(chat).queryByRole("button")).toBeNull();
  });
});
