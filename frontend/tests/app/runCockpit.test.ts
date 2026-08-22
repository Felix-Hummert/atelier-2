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
  completedRun,
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
  it("proves(a-started-run-shows-the-working-node-live): a STARTED V1 run keeps the working card live and the event ticker open", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api({ openRunEvents: feed.open, getRun: vi.fn(async () => startedRun()) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const working = await screen.findByRole("article", { name: "agent — Working" });
    expect(working.classList.contains("live-work")).toBe(true);
    expect(working.getAttribute("data-live")).toBe("true");
    expect(screen.getByText("Process log stays in the lease.").isConnected).toBe(true);
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(document.querySelector("details.event-log")?.hasAttribute("open")).toBe(true);
    expect(screen.getByText("No durable events yet.").isConnected).toBe(true);

    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    await waitFor(() => expect(screen.getAllByText("AGENT COMPLETED").length).toBeGreaterThan(0));
    expect(screen.getByText("Process log stays in the lease.").isConnected).toBe(true);
  });

  it("loads one authoritative run plus bound graph before opening the durable event history", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api({ openRunEvents: feed.open, getRun: vi.fn(async () => waitingInputRun()) });

    render(App, { props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) } });

    expect((await screen.findByRole("heading", { name: "Unnamed workflow" })).isConnected).toBe(true);
    expect(cockpitApi.getWorkflowRevision).toHaveBeenCalledWith(digest);
    expect(feed.open).toHaveBeenCalledWith(publicReference, expect.any(Object));
    expect(screen.getByRole("article", { name: "agent — Done" }).textContent).toContain("Build it");
    expect(screen.getByRole("article", { name: "action — Done" }).isConnected).toBe(true);
    expect(screen.getByRole("article", { name: "wait — Needs you" }).isConnected).toBe(true);
    expect(screen.getByRole("article", { name: "final — Queued" }).isConnected).toBe(true);
    expect(screen.getByText("No durable events yet.").isConnected).toBe(true);
  });

  it("retains confirmed nodes and events through a disconnect the live stream heals on its own", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api({ openRunEvents: feed.open, getRun: vi.fn(async () => startedRun()) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: "Unnamed workflow" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(agentCompleted(1)));
    feed.handlers?.disconnected();

    expect(await screen.findByText("Reconnecting")).toBeTruthy();
    await waitFor(() => expect(screen.getAllByText("AGENT COMPLETED")).toHaveLength(2));
    // A dropped connection is the browser's own EventSource retrying: a second,
    // manual freshness control here would just compete with that one honest
    // model (#506), so none is offered while it is merely reconnecting.
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();

    feed.handlers?.opened();
    await waitFor(() => expect(screen.getByText("Live")).toBeTruthy());
    expect(screen.getByRole("article", { name: "agent — Done" }).isConnected).toBe(true);
  });

  it("stops a gapped stream without replacing the last confirmed event", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api({ openRunEvents: feed.open, getRun: vi.fn(async () => startedRun()) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: "Unnamed workflow" });
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
    await screen.findByRole("heading", { name: "Unnamed workflow" });

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

  it("retries a stopped stream without ever clearing its confirmed event truth", async () => {
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
    await screen.findByRole("heading", { name: "Unnamed workflow" });
    const raw = JSON.stringify(agentCompleted(1));
    feed.handlers?.event(raw);
    feed.handlers?.event(JSON.stringify(actionCompleted(3)));
    await screen.findByText("Event gap");

    // A protocol violation is the one honest case the live stream cannot heal
    // on its own (#506): the named "Retry" affordance beside the stopped
    // status is what reopens it.
    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));
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
    expect(screen.queryByRole("heading", { name: "Unnamed workflow" })).toBeNull();
  });

  it("proves(a-run-page-hash-is-a-named-proof-anchor): a V1 summary hash leads with its name and copies on click", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(App, {
      props: {
        cockpitApi: api({ getRun: vi.fn(async () => completedRun()) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: "Unnamed workflow" });
    expect(screen.queryByText(digest)).toBeNull();
    const workflow = screen.getByRole("button", { name: "Workflow revision" });
    expect(workflow.textContent).not.toContain(digest);
    workflow.focus();
    expect(document.activeElement).toBe(workflow);
    await fireEvent.click(workflow);
    expect(writeText).toHaveBeenCalledWith(digest);
    await waitFor(() => expect(screen.getByText("Copied").isConnected).toBe(true));

    const terminal = screen.getByRole("button", { name: "Terminal hash" });
    await fireEvent.click(terminal);
    expect(writeText).toHaveBeenLastCalledWith(digest);
  });

  it("names the header with the workflow's honest state, with the run id anchored beside it", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(App, {
      props: {
        cockpitApi: api({ getRun: vi.fn(async () => completedRun()) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    // Only a V3 document declares a workflow name; this V1 run has none to
    // read, so the header says that honestly rather than titling itself with
    // the raw run id (the Constitution's law 8 counterexample).
    const title = await screen.findByRole("heading", { level: 1, name: "Unnamed workflow" });
    expect(title.isConnected).toBe(true);
    const identity = screen.getByRole("button", { name: "Run id" });
    expect(identity.textContent).not.toContain("run");
    await fireEvent.click(identity);
    expect(writeText).toHaveBeenCalledWith("run");
    await waitFor(() => expect(screen.getByText("Copied").isConnected).toBe(true));
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
