import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  executableGraph,
  type CockpitApi,
  type Run
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import {
  actionCompleted,
  agentCompleted,
  agentCompletedRun,
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

describe("read-only run cockpit", () => {
  it("loads one authoritative run plus bound graph before opening the durable event history", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api({ openRunEvents: feed.open, getRun: vi.fn(async () => waitingInputRun()) });

    render(App, { props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) } });

    expect((await screen.findByRole("heading", { name: "Run run" })).isConnected).toBe(true);
    expect(cockpitApi.getWorkflowRevision).toHaveBeenCalledWith(digest);
    expect(feed.open).toHaveBeenCalledWith(publicReference, expect.any(Object));
    expect(screen.getByRole("article", { name: "agent — Done" }).textContent).toContain("Build it");
    expect(screen.getByRole("article", { name: "action — Done" }).isConnected).toBe(true);
    expect(screen.getByRole("article", { name: "wait — Needs you" }).isConnected).toBe(true);
    expect(screen.getByRole("article", { name: "final — Queued" }).isConnected).toBe(true);
    expect(screen.getByText("No durable events yet.").isConnected).toBe(true);
  });

  it("retains confirmed nodes and events through disconnect and a later typed API failure", async () => {
    const feed = new FakeRunEventFeed();
    const getRun = vi.fn().mockResolvedValueOnce(startedRun());
    const cockpitApi = api({ openRunEvents: feed.open, getRun });
    render(App, { props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) } });
    await screen.findByRole("heading", { name: "Run run" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    feed.handlers?.disconnected();

    expect(await screen.findByText("Reconnecting")).toBeTruthy();
    await waitFor(() => expect(screen.getAllByText("AGENT COMPLETED")).toHaveLength(2));
    getRun.mockRejectedValueOnce(
      new CockpitRequestError("Read failed", {
        type: "urn:atelier2:problem:v1:temporarily-unavailable",
        title: "Temporarily unavailable",
        status: 503,
        detail: "The durable store cannot be read right now."
      })
    );
    await fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByText("Temporarily unavailable")).toBeTruthy();
    expect(screen.getByText("The durable store cannot be read right now.").isConnected).toBe(true);
    expect(screen.getByRole("article", { name: "agent — Done" }).isConnected).toBe(true);
    expect(screen.getAllByText("AGENT COMPLETED")).toHaveLength(2);
  });

  it("stops a gapped stream without replacing the last confirmed event", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api({ openRunEvents: feed.open, getRun: vi.fn(async () => startedRun()) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: "Run run" });
    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    feed.handlers?.event(JSON.stringify(actionCompleted(3)));

    expect(await screen.findByText("Event gap")).toBeTruthy();
    expect(screen.getAllByText("AGENT COMPLETED")).toHaveLength(2);
    expect(feed.close).toHaveBeenCalled();
  });

  it("reloads the visible run snapshot after a durable Wait answer advances the run", async () => {
    const feed = new FakeRunEventFeed();
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(waitingInputRun())
      .mockResolvedValueOnce(waitingInputRun())
      .mockResolvedValueOnce(answeredRun());
    render(App, {
      props: {
        cockpitApi: api({ openRunEvents: feed.open, getRun }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: "Run run" });

    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    feed.handlers?.event(JSON.stringify(actionCompleted(2)));
    feed.handlers?.event(JSON.stringify(waitingInput(3)));
    await waitFor(() => expect(getRun).toHaveBeenCalledTimes(2));
    expect(screen.getByText("waiting input").isConnected).toBe(true);

    feed.handlers?.event(JSON.stringify(waitAnswered(4)));

    expect(await screen.findByText("started")).toBeTruthy();
    expect(getRun).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("article", { name: "wait — Done" }).isConnected).toBe(true);
    expect(screen.getByRole("article", { name: "final — Working" }).isConnected).toBe(true);
  });

  it("refreshes a failed stream without ever clearing its confirmed event truth", async () => {
    const feed = new FakeRunEventFeed();
    let resolveRefresh!: (run: Run) => void;
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(startedRun())
      .mockImplementationOnce(() => new Promise<Run>((resolve) => { resolveRefresh = resolve; }));
    render(App, {
      props: {
        cockpitApi: api({ openRunEvents: feed.open, getRun }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: "Run run" });
    const raw = JSON.stringify(agentCompleted(1));
    feed.handlers?.event(raw);
    feed.handlers?.event(JSON.stringify(actionCompleted(3)));
    await screen.findByText("Event gap");

    await fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expectOneConfirmedAgentEvent();
    resolveRefresh(agentCompletedRun());
    await waitFor(() => expect(feed.open).toHaveBeenCalledTimes(2));
    expectOneConfirmedAgentEvent();

    feed.handlers?.event(raw);
    expectOneConfirmedAgentEvent();
    expect(screen.queryByText("Event conflict")).toBeNull();
    feed.handlers?.event(`${raw} `);

    expect(await screen.findByText("Event conflict")).toBeTruthy();
    expectOneConfirmedAgentEvent();
  });

  it("shows honest loading and projects an initial RFC 9457 failure", async () => {
    let rejectRun!: (error: unknown) => void;
    const getRun = vi.fn(
      () => new Promise<Run>((_resolve, reject) => { rejectRun = reject; })
    );
    render(App, {
      props: { cockpitApi: api({ getRun }), mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(screen.getByRole("status").textContent).toBe("Looking…");
    rejectRun(
      new CockpitRequestError("Missing", {
        type: "urn:atelier2:problem:v1:run-not-found",
        title: "Run not found",
        status: 404,
        detail: "No durable run has this reference."
      }, true)
    );

    expect((await screen.findByText("Run not found")).isConnected).toBe(true);
    expect(screen.getByText("No durable run has this reference.").isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Run run" })).toBeNull();
  });

  it("does not open event history for a run whose current node disagrees with its revision", async () => {
    const feed = new FakeRunEventFeed();
    const mismatchedRevision = revision();
    const mismatchedGraph = executableGraph(mismatchedRevision.graph);
    const firstNode = mismatchedGraph.nodes[0];
    if (firstNode?.type !== "agent") {
      throw new Error("Expected the fixture to start with an Agent node.");
    }
    mismatchedGraph.nodes[0] = {
      ...firstNode,
      job: "Different work"
    };
    render(App, {
      props: {
        cockpitApi: api({
          openRunEvents: feed.open,
          getWorkflowRevision: vi.fn(async () => mismatchedRevision)
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect((await screen.findByText("Run unavailable")).isConnected).toBe(true);
    expect(feed.open).not.toHaveBeenCalled();
  });
});

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    getRun: vi.fn(async () => startedRun()),
    getWorkflowRevision: vi.fn(async () => revision()),
    ...overrides
  });
}

function expectOneConfirmedAgentEvent(): void {
  expect(screen.getByText("Events", { exact: false, selector: "summary" }).textContent).toContain("1");
  expect(screen.queryByText("No durable events yet.")).toBeNull();
  expect(screen.getByRole("article", { name: "agent — Done" }).isConnected).toBe(true);
  expect(screen.getByRole("article", { name: "action — Working" }).isConnected).toBe(true);
  expect(screen.getAllByText("AGENT COMPLETED")).toHaveLength(2);
}
