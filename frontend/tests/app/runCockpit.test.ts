import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  type CockpitApi,
  type Run,
  type RunV1,
  type RunEventHandlers,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";

const digest = "a".repeat(64);
const publicReference = "run1.cnVu";

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
});

afterEach(() => cleanup());

describe("read-only run cockpit", () => {
  it("loads one authoritative run plus bound graph before opening the durable event history", async () => {
    const feed = new FakeFeed();
    const cockpitApi = api({ openRunEvents: feed.open, getRun: vi.fn(async () => waitingRun()) });

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
    const feed = new FakeFeed();
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
    const feed = new FakeFeed();
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
    expect(feed.closed).toBe(true);
  });

  it("reloads the visible run snapshot after a durable Wait answer advances the run", async () => {
    const feed = new FakeFeed();
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValueOnce(afterAnswerRun());
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
    const feed = new FakeFeed();
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
    resolveRefresh(actionStartedRun());
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
    const feed = new FakeFeed();
    const mismatchedRevision = revision();
    const firstNode = mismatchedRevision.graph.nodes[0];
    if (firstNode?.type !== "agent") {
      throw new Error("Expected the fixture to start with an Agent node.");
    }
    mismatchedRevision.graph.nodes[0] = {
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

class FakeFeed {
  handlers: RunEventHandlers | null = null;
  closed = false;
  open = vi.fn((_publicReference: string, handlers: RunEventHandlers) => {
    this.handlers = handlers;
    return { close: () => { this.closed = true; } };
  });
}

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return {
    listRuns: vi.fn(async () => ({ items: [], next_after: null })),
    listWorkflowRevisions: vi.fn(async () => ({ items: [], next_after_revision_hash: null })),
    publish: vi.fn(),
    publishAuthProfile: vi.fn(),
    publishAgentConfiguration: vi.fn(),
    start: vi.fn(),
    answer: vi.fn(),
    reconcile: vi.fn(),
    getRun: vi.fn(async () => startedRun()),
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

function startedRun(): RunV1 {
  return {
    run_id: "run",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    state_version: 0,
    state: "STARTED",
    current_node: revision().graph.nodes[0]! as RunV1["current_node"],
    waiting: { type: "NONE" },
    terminal_hash: null,
    latest_event_cursor: null
  };
}

function waitingRun(): RunV1 {
  return {
    ...startedRun(),
    state_version: 3,
    state: "WAITING_INPUT",
    current_node: revision().graph.nodes[2]! as RunV1["current_node"],
    waiting: { type: "WAITING_INPUT", node_id: "wait", answer_type: "integer" },
    latest_event_cursor: "event1.cnVu.3"
  };
}

function actionStartedRun(): RunV1 {
  return {
    ...startedRun(),
    state_version: 1,
    current_node: revision().graph.nodes[1]! as RunV1["current_node"],
    latest_event_cursor: "event1.cnVu.1"
  };
}

function afterAnswerRun(): RunV1 {
  return {
    ...startedRun(),
    state_version: 4,
    current_node: revision().graph.nodes[3]! as RunV1["current_node"],
    latest_event_cursor: "event1.cnVu.4"
  };
}

function agentCompleted(sequence: number) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: "agent",
    node_execution_id: digest,
    event_hash: digest,
    event: "AGENT_COMPLETED",
    output: "candidate T",
    payload_hash: digest
  };
}

function actionCompleted(sequence: number) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: "action",
    node_execution_id: digest,
    event_hash: digest,
    event: "ACTION_COMPLETED",
    receipt: {
      logical_effect_key: "effect",
      request_hash: digest,
      effect_id: "effect-1",
      result_hash: digest,
      result_base64: "cmVzdWx0",
      confirmation_source: "ADAPTER_EXECUTION",
      reconcile_command_id: null
    }
  };
}

function waitingInput(sequence: number) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: "wait",
    node_execution_id: digest,
    event_hash: digest,
    event: "WAITING_INPUT",
    answer_type: "integer"
  };
}

function waitAnswered(sequence: number) {
  return {
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: "wait",
    node_execution_id: digest,
    event_hash: digest,
    event: "WAIT_ANSWERED",
    answer: "5",
    answer_hash: digest
  };
}

function expectOneConfirmedAgentEvent(): void {
  expect(screen.getByText("Events", { exact: false, selector: "summary" }).textContent).toContain("1");
  expect(screen.queryByText("No durable events yet.")).toBeNull();
  expect(screen.getByRole("article", { name: "agent — Done" }).isConnected).toBe(true);
  expect(screen.getByRole("article", { name: "action — Working" }).isConnected).toBe(true);
  expect(screen.getAllByText("AGENT COMPLETED")).toHaveLength(2);
}
