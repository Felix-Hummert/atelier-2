import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  type AnyRun,
  type CockpitApi,
  type Problem,
  type RunV1,
  type RunV3
} from "../../src/api/client";
import {
  reportConnectionLost,
  reportConnectionRestored,
  restartNoticeCopy
} from "../../src/lib/connectionState";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { standingWords } from "../../src/lib/runState";
import { studioPageCopy } from "../../src/lib/studioPageCopy";
import {
  describeStudioControl,
  questionForStudioControl,
  studioInteractiveSelector,
  studioQuestions,
  studioStageSelector,
  unansweredStudioControls
} from "../../src/lib/studioQuestions";
import { boardBadgeCounts } from "../../src/lib/workshop";
import { cockpitApiStub, FakeRunEventFeed, PAGE_CURSORS } from "../support/cockpitApi";
import { notCancellableBlock } from "../support/runV3";
import {
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
  boardBadgeCounts.set(null);
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  reportConnectionRestored();
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
    throw new Error("Board stage is missing");
  }
  const unanswered = unansweredStudioControls(stage);
  expect(
    unanswered.map(describeStudioControl),
    unanswered.map(describeStudioControl).join("; ")
  ).toEqual([]);
  const present = [...stage.querySelectorAll(studioInteractiveSelector)].map((element) => {
    const found = questionForStudioControl(element);
    if (found === null) {
      throw new Error(`unmapped Board control: ${describeStudioControl(element)}`);
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
    cancellation: notCancellableBlock("between-nodes"),
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

describe("the board is the level the workshop opens on", () => {
  // The identifier stays "studio": REQ-UI-01 in docs/requirements/0003-ziel-ui.md
  // still declares it under that name, and its wording is #521's revision, not
  // this slice's.
  it("proves(the-workshop-opens-in-the-studio): opens the bare atelier path in the Board instead of a list of runs", async () => {
    openStudio();

    expect((await screen.findByRole("heading", { name: "Board" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Runs" })).toBeNull();
    expect(window.location.pathname).toBe("/atelier");
  });

  it("asks the durable list by the states the board's groups can name, and reads the workflow catalog", async () => {
    const listRuns = listRunsByState([startedRun()]);
    const listWorkflowRevisions = vi.fn(async () => ({ items: [], next_after_revision_hash: null }));
    openStudio([startedRun()], { listRuns, listWorkflowRevisions });
    await screen.findByRole("region", { name: "Running · 1" });

    expect(listRuns.mock.calls.map(([, state]) => state).sort()).toEqual([
      "STARTED",
      "WAITING_INPUT",
      "WAITING_RECONCILIATION"
    ]);
    expect(listWorkflowRevisions).toHaveBeenCalled();
  });

  it("names a row by the catalog's workflow name, and falls back to the run id honestly when the catalog names nothing", async () => {
    openStudio(
      [startedRun({ public_run_reference: "run1.YQ", run_id: "named", workflow_revision_hash: "b".repeat(64) }),
       startedRun({ public_run_reference: "run1.Yg", run_id: "unnamed" })],
      {
        listWorkflowRevisions: vi.fn(async () => ({
          items: [
            {
              workflow_revision_hash: "b".repeat(64),
              workflow_format_version: 1 as const,
              executable: true,
              not_executable_reason: null,
              name: "Preview door",
              description: null
            },
            {
              workflow_revision_hash: revisionHash,
              workflow_format_version: 1 as const,
              executable: true,
              not_executable_reason: null,
              name: null,
              description: null
            }
          ],
          next_after_revision_hash: null
        }))
      }
    );

    const running = await screen.findByRole("region", { name: "Running · 2" });
    expect(within(running).getByText("Preview door").isConnected).toBe(true);
    expect(within(running).getByText("unnamed").isConnected).toBe(true);
  });

  it("keeps real board work ahead of an empty first paint", async () => {
    openStudio([waitingInputRun(), startedRun({ public_run_reference: "run1.YQ" })]);

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    const running = await screen.findByRole("region", { name: "Running · 1" });

    expect(needsYou.compareDocumentPosition(running) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });
});

describe("Needs you names what waits for a human", () => {
  // Same identifier note as above: REQ-UI-01 still declares this sentence as
  // "the-inbox-names-...", the wording carried by #521.
  it("proves(the-inbox-names-every-run-that-waits-for-a-human): names every run in a durable waiting state and no run that waits for nobody, across every page the list holds", async () => {
    // "Across everything" is only true while the reading spans the durable
    // pages: a run that waits on the second page is exactly the one a
    // single-page read would lose.
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

    const needsYou = await screen.findByRole("region", { name: "Needs you · 3" });

    const waiting = within(needsYou).getAllByRole("link");
    expect(waiting).toHaveLength(3);
    expect(within(needsYou).getAllByText("Answer →")).toHaveLength(2);
    expect(within(needsYou).getByText("Reconcile →").isConnected).toBe(true);
  });

  it("shows no Needs you section when nothing waits for a human", async () => {
    openStudio([startedRun()]);

    await screen.findByRole("region", { name: "Running · 1" });

    expect(screen.queryByRole("region", { name: /Needs you/ })).toBeNull();
  });

  it("opens the waiting run with one click", async () => {
    openStudio([waitingInputRun()], { getRun: vi.fn(async () => waitingInputRun()) });

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    await fireEvent.click(within(needsYou).getByRole("link", { name: /Answer/ }));

    await waitFor(() => expect(window.location.pathname).toBe("/atelier/runs/run1.cnVu"));
  });
});

describe("Running holds only what still moves, never a landed result", () => {
  it("names the node a running row is at, from the run's current node", async () => {
    openStudio([startedRun()]);

    const running = await screen.findByRole("region", { name: "Running · 1" });
    expect(within(running).getByText(`${standingWords.running} · agent`).isConnected).toBe(true);
  });
});

describe("the Board never lists a terminal run -- it belongs to History instead (#581, #667)", () => {
  it("does not list a failed run's state at all, so a failed run stays off the Board entirely", async () => {
    const listRuns = listRunsByState([
      startedRun({ public_run_reference: "run1.YQ" }),
      failedRun({ public_run_reference: "run1.Yg" })
    ]);
    openStudio([], { listRuns });

    const running = await screen.findByRole("region", { name: "Running · 1" });
    expect(within(running).getAllByRole("link")).toHaveLength(1);
    expect(listRuns.mock.calls.some(([, state]) => state === "FAILED")).toBe(false);
    expect(screen.queryByText(standingWords.failed)).toBeNull();
  });
});

describe("the mini pipeline reads node_rail honestly", () => {
  it("shows one dot per node_rail entry for a V2/V3 run", async () => {
    openStudio([listedV3Run({ node_rail: [
      { node_id: "plan", state: "succeeded", attempt: null },
      { node_id: "build", state: "working", attempt: null }
    ] })]);

    const running = await screen.findByRole("region", { name: "Running · 1" });
    const row = within(running).getAllByRole("link")[0] as HTMLElement;
    expect(row.querySelectorAll(".pipe-dot")).toHaveLength(2);
  });

  it("shows no mini pipeline for a V1 run, which carries no node_rail", async () => {
    openStudio([startedRun()]);

    const running = await screen.findByRole("region", { name: "Running · 1" });
    const row = within(running).getAllByRole("link")[0] as HTMLElement;
    expect(row.querySelectorAll(".pipe-dot")).toHaveLength(0);
  });
});

describe("there is no Queued group", () => {
  it("never renders a Queued section, because no served run state names one", async () => {
    openStudio([startedRun(), waitingInputRun({ public_run_reference: "run1.YQ" })]);

    await screen.findByRole("region", { name: "Running · 1" });

    expect(screen.queryByText(/Queued/)).toBeNull();
  });
});

describe("the rail badges show the Board's last confirmed read", () => {
  it("shows no badge before the first read, and the read counts after it", async () => {
    openStudio([waitingInputRun(), startedRun({ public_run_reference: "run1.YQ" })]);
    const rail = screen.getByRole("navigation", { name: "Workshop" });
    const board = within(rail).getByRole("link", { name: /Board/ });
    expect(within(board).queryByText("1")).toBeNull();

    await screen.findByRole("region", { name: "Needs you · 1" });

    expect(within(board).getByText("1", { selector: ".rail-badge-running" }).isConnected).toBe(true);
    expect(within(board).getByText("1", { selector: ".rail-badge-needs-you" }).isConnected).toBe(true);
  });

  it("keeps the last known Board count visible on another page", async () => {
    openStudio([waitingInputRun()]);
    await screen.findByRole("region", { name: "Needs you · 1" });

    window.history.pushState(null, "", "/atelier/project");
    window.dispatchEvent(new PopStateEvent("popstate"));

    const rail = screen.getByRole("navigation", { name: "Workshop" });
    const board = within(rail).getByRole("link", { name: /Board/ });
    expect(within(board).getByText("1", { selector: ".rail-badge-needs-you" }).isConnected).toBe(true);
  });
});

describe("an empty board teaches the one next action", () => {
  it("proves(an-empty-area-names-the-one-next-action): names starting a run as the one action possible today, and offers it once", async () => {
    const { feed } = openStudioHolding([]);
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();

    const empty = await screen.findByRole("heading", { name: studioPageCopy.emptyTitle });

    expect(empty.isConnected).toBe(true);
    // A healthy stream says nothing at all: a permanent badge is chrome, and
    // the empty state itself proves the board is following (operator, 23.08.).
    expect(screen.queryByText("Live")).toBeNull();
    expect(screen.queryByText("Connecting")).toBeNull();
    expect(screen.getAllByRole("link", { name: studioPageCopy.emptyStart })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("link", { name: studioPageCopy.emptyStart }));

    expect((await screen.findByRole("heading", { name: "Workflows" })).isConnected).toBe(true);
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

    expect((await screen.findByText("Board runs unavailable")).isConnected).toBe(true);
    expect(screen.queryByText(/raw transport failure|private adapter failure|Failed to fetch/))
      .toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry board runs" })).toHaveLength(1);
  });

  it("names no local failure while the whole workshop reads unreachable, and reads itself again once the connection returns (#700)", async () => {
    const listRuns = vi.fn().mockRejectedValue(new Error("Failed to fetch"));
    openStudio([], { listRuns });
    await screen.findByRole("heading", { name: "Board" });

    reportConnectionLost();
    await waitFor(() => {
      expect(document.querySelector(".notice-banner")?.textContent).toContain(restartNoticeCopy);
    });
    // The shell's one line above already names the outage; this room adds no
    // second, page-local echo of the same fact.
    expect(screen.queryByText(studioPageCopy.runsUnavailable)).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry board runs" })).toBeNull();

    listRuns.mockImplementation(listRunsByState([startedRun()]));
    reportConnectionRestored();

    expect((await screen.findByRole("region", { name: "Running · 1" })).isConnected).toBe(true);
    expect(screen.queryByText(studioPageCopy.runsUnavailable)).toBeNull();
  });

  it("repeats only the failed Board read until a successful retry replaces the error", async () => {
    const listRuns = vi.fn(async (_after?: string, state?: string) => {
      const round = Math.floor((listRuns.mock.calls.length - 1) / 3);
      if (round < 2) throw new Error("socket detail must stay private");
      return {
        items: state === "STARTED" ? [startedRun()] : [],
        next_after: null
      };
    });
    openStudio([], { listRuns });

    await screen.findByText("Board runs unavailable");
    // A fresh query per click, never a held reference: each failed round
    // mounts its own Retry control (ReadState.svelte's pattern for #514),
    // so the operator sees and clicks whatever Retry is on screen right now.
    await fireEvent.click(screen.getByRole("button", { name: "Retry board runs" }));
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(6));
    await screen.findByRole("button", { name: "Retry board runs" });
    expect(screen.getAllByRole("button", { name: "Retry board runs" })).toHaveLength(1);
    expect(screen.queryByText(/socket detail/)).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Retry board runs" }));

    expect((await screen.findByRole("region", { name: "Running · 1" })).isConnected).toBe(true);
    expect(listRuns).toHaveBeenCalledTimes(9);
    expect(screen.queryByRole("button", { name: /board runs/ })).toBeNull();
    expect(window.location.pathname).toBe("/atelier");
  });

  it("confirms the run list on a failed catalog read, falling back to run ids and naming the gap", async () => {
    openStudio([startedRun()], {
      listWorkflowRevisions: vi.fn(async (after?: string) => {
        if (after === undefined) {
          return { items: [], next_after_revision_hash: "c".repeat(64) };
        }
        throw new Error("later catalog page detail");
      })
    });

    const running = await screen.findByRole("region", { name: "Running · 1" });
    expect(within(running).getByText("run").isConnected).toBe(true);
    expect(screen.getByText("Workflow names unavailable — showing run ids.").isConnected).toBe(true);
    expect(screen.queryByText("Board runs incomplete")).toBeNull();
    expect(screen.queryByText(/later catalog page detail/)).toBeNull();
  });

  it("offers no manual refresh once the Board read is confirmed -- only the live indicator names the read's freshness", async () => {
    openStudio([startedRun()]);

    await screen.findByRole("region", { name: "Running · 1" });

    expect(screen.queryByRole("button", { name: /board runs/ })).toBeNull();
  });

  it("does not confirm a partial initial three-list reading", async () => {
    const listRuns = vi.fn(async (after?: string, state?: string) => {
      if (state === "STARTED" && after === undefined) {
        return { items: [startedRun()], next_after: "run1.bmV4dA" };
      }
      if (state === "STARTED") throw new Error("later page detail");
      return { items: [], next_after: null };
    });
    openStudio([], { listRuns });

    expect((await screen.findByText("Board runs incomplete")).isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: /Running/ })).toBeNull();
    expect(screen.queryByRole("heading", { name: studioPageCopy.emptyTitle })).toBeNull();
    expect(screen.queryByText(/later page detail/)).toBeNull();
  });
});

describe("the board holds GET /events", () => {
  it("holds the attention stream when the board opens", async () => {
    const { feed } = openStudioHolding([]);

    await screen.findByRole("heading", { name: "Board" });

    expect(feed.openAttention).toHaveBeenCalledWith(expect.any(Object));
  });

  it("withholds the empty board until the stream is actually following", async () => {
    openStudioHolding([]);

    await waitFor(() => expect(screen.queryByText("Looking…")).toBeNull());
    // Nothing has confirmed that nothing is running, so the board does not say it.
    expect(screen.queryByRole("heading", { name: studioPageCopy.emptyTitle })).toBeNull();
  });

  it("applies a WAITING_INPUT from the stream without already listing the run", async () => {
    const waiting = waitingInputRun({ public_run_reference: "run1.YQ" });
    const getRun = vi.fn(async () => waiting);
    const { feed } = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: studioPageCopy.emptyTitle });

    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    expect(within(needsYou).getByRole("link", { name: /Answer/ }).isConnected).toBe(true);
    expect(getRun).toHaveBeenCalledWith("run1.YQ");
    expect(screen.queryByRole("heading", { name: studioPageCopy.emptyTitle })).toBeNull();
  });

  it("applies an AGENT_FAILED from the stream by removing the run from the Board, not by showing it done (#667)", async () => {
    const getRun = vi.fn(async () => failedRun());
    const { feed } = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: studioPageCopy.emptyTitle });

    feed.handlers?.event(JSON.stringify(agentFailedEvent()));

    await waitFor(() => expect(getRun).toHaveBeenCalledWith(publicReference));
    // The failed run turned terminal, so it leaves the Board for History
    // (#667) instead of landing in a "Done" group here.
    await screen.findByRole("heading", { name: studioPageCopy.emptyTitle });
    expect(screen.queryByRole("region", { name: /Running/ })).toBeNull();
    expect(screen.queryByRole("region", { name: /Needs you/ })).toBeNull();
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
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();

    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    expect(within(needsYou).getByRole("link", { name: /Answer/ }).isConnected).toBe(true);

    releaseStarted({ items: [staleStarted, otherStarted], next_after: null });
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Running · 1" }).isConnected).toBe(true);
    });
    expect(screen.getByRole("region", { name: "Needs you · 1" }).isConnected).toBe(true);
  });

  it("replaces a projected wait when the attention stream later reports the same run failed, at a newer version", async () => {
    const waiting = waitingInputRun();
    const newerFailed = failedRun({ state_version: 4 });
    const getRun = vi.fn(async () => waiting);
    const { feed } = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();

    feed.handlers?.event(JSON.stringify(waitingInput(1)));

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    expect(within(needsYou).getByRole("link", { name: /Answer/ }).isConnected).toBe(true);

    getRun.mockResolvedValueOnce(newerFailed);
    feed.handlers?.event(JSON.stringify(agentFailedEvent()));

    // The run turned terminal, so it leaves the Board for History (#667)
    // instead of replacing the wait with a Done row here.
    await waitFor(() => expect(screen.queryByRole("region", { name: /Needs you/ })).toBeNull());
    expect(screen.queryByRole("region", { name: /Running/ })).toBeNull();
  });

  it("retries a failed getRun until the delivered wait is visible once", async () => {
    const waiting = waitingInputRun({ public_run_reference: "run1.YQ" });
    const getRun = vi
      .fn()
      .mockRejectedValueOnce(new Error("run missing"))
      .mockResolvedValueOnce(waiting);
    const { feed } = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: studioPageCopy.emptyTitle });

    feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );

    expect((await screen.findByText("run missing")).isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: /Needs you/ })).toBeNull();
    expect(screen.queryAllByRole("link", { name: /Start/ })).toHaveLength(0);
    expect(getRun).toHaveBeenCalledTimes(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    expect(within(needsYou).getAllByRole("link", { name: /Answer/ })).toHaveLength(1);
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
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: studioPageCopy.emptyTitle });

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
    expect(feed.close).toHaveBeenCalled();

    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    const needsYou = await screen.findByRole("region", { name: "Needs you · 1" });
    expect(within(needsYou).getAllByRole("link", { name: /Answer/ })).toHaveLength(1);
    expect(screen.getByText("Stopped").isConnected).toBe(true);
    expect(screen.queryAllByRole("link", { name: /Start/ })).toHaveLength(0);
  });

  it("names a failed attention stream as itself, not as an empty board", async () => {
    const { feed } = openStudioHolding([]);
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: studioPageCopy.emptyTitle });

    feed.handlers?.event(JSON.stringify(streamFailedFrame()));

    expect((await screen.findByText("Stopped")).isConnected).toBe(true);
    expect(screen.getByText("Durable state is corrupt").isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: studioPageCopy.emptyTitle })).toBeNull();
    expect(screen.queryByText("Connecting")).toBeNull();
    expect(screen.queryAllByRole("link", { name: /Start/ })).toHaveLength(0);
    expect(feed.close).toHaveBeenCalled();
  });
});

describe("every Board control answers a named user question", () => {
  // The identifier stays "studio" (acceptance/435): its wording is #521's revision.
  it("proves(studio-elements-answer-named-questions): every interactive Board control is listed against one named user question", async () => {
    const ids = Object.values(studioQuestions).map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const entry of Object.values(studioQuestions)) {
      expect(entry.question.endsWith("?")).toBe(true);
    }

    openStudio([
      startedRun({ public_run_reference: "run1.YQ" }),
      waitingInputRun({ public_run_reference: "run1.Yw" })
    ]);
    await screen.findByRole("region", { name: "Running · 1" });
    await screen.findByRole("region", { name: "Needs you · 1" });
    expectStudioControlsAnswerNamedQuestions([studioQuestions.openRun.id]);

    cleanup();
    const { feed } = openStudioHolding([]);
    await screen.findByRole("heading", { name: "Board" });
    feed.handlers?.opened();
    await screen.findByRole("link", { name: studioPageCopy.emptyStart });
    expectStudioControlsAnswerNamedQuestions([studioQuestions.emptyStart.id]);

    cleanup();
    openStudio([], {
      listRuns: vi.fn().mockRejectedValue(new Error("wire detail"))
    });
    await screen.findByRole("button", { name: "Retry board runs" });
    expectStudioControlsAnswerNamedQuestions([studioQuestions.reloadStudioRuns.id]);

    cleanup();
    const getRun = vi.fn().mockRejectedValueOnce(new Error("run missing"));
    const projection = openStudioHolding([], { getRun });
    await screen.findByRole("heading", { name: "Board" });
    projection.feed.handlers?.opened();
    await screen.findByRole("heading", { name: studioPageCopy.emptyTitle });
    projection.feed.handlers?.event(
      JSON.stringify(
        waitingInput(1, { public_run_reference: "run1.YQ", cursor: "event1.YQ.1" })
      )
    );
    expect((await screen.findByText("run missing")).isConnected).toBe(true);
    expectStudioControlsAnswerNamedQuestions([studioQuestions.retryProjection.id]);
  });
});
