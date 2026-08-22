import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { CockpitRequestError, type CockpitApi, type RunV3 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { eventCursor, publicReference, revisionHash as digest } from "../support/workflowV1";

const configurationHash = "c".repeat(64);
const terminalHash = "d".repeat(64);

function v3Revision() {
  return {
    workflow_revision_hash: digest,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3 as const,
      executable: true as const,
      not_executable_reason: null,
      node_count: 2,
      agent_roles: ["builder"],
      orders: [],
      node_previews: [
        {
          id: "implement",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Do the one thing this chain is for.",
          depends_on: []
        },
        {
          id: "review",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Check what the node before you did.",
          depends_on: ["implement"]
        }
      ],
      loops: [],
      name: "Two agents in a line",
      description: null
    }
  };
}

function v3Run(overrides: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/two-agents",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: configurationHash,
    agent_bindings: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [
      { node_id: "implement", state: "succeeded", attempt: null },
      { node_id: "review", state: "working", attempt: null }
    ],
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: null,
    ...overrides
  };
}

function api(run: RunV3, overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    getRun: vi.fn(async () => run),
    getWorkflowRevision: vi.fn(async () => v3Revision()),
    ...overrides
  });
}

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
});

afterEach(() => cleanup());

describe("a version 3 run in the cockpit", () => {
  it("proves(a-v3-run-is-visible-in-the-cockpit): shows the line, which node is running, and that nothing has ended yet", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(
      (await screen.findByRole("heading", { level: 1, name: "Two agents in a line" })).isConnected
    ).toBe(true);
    const graph = await screen.findByRole("region", { name: "Workflow" });
    expect(within(graph).getByRole("button", { name: "implement — Done" }).isConnected).toBe(true);
    expect(within(graph).getByRole("button", { name: "review — Working" }).isConnected).toBe(true);
    expect(screen.getByText(/not yet/i).isConnected).toBe(true);
    expect(screen.queryByText(configurationHash)).toBeNull();
    expect(screen.getByRole("button", { name: "Run configuration" }).isConnected).toBe(true);
    expect(
      screen.getByRole("button", { name: "Run configuration" }).getAttribute("title")
    ).toBeNull();
    // A loaded run is not a failed one: the page must not offer to fetch it again
    // beneath the answer it already has.
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("proves(a-chain-run-is-watched-while-it-runs): follows the run live and says which node just finished", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(
      JSON.stringify(await completedEvent("implement", "the draft", 1))
    );

    // This page said for one head that it was NOT following live, and that was
    // true: no format-3 event existed. #249 put one on the wire, so the claim
    // became the thing that was untrue, and the assertion moves with it.
    await waitFor(() =>
      expect(
        screen.getByLabelText("Where this run stands").textContent
      ).toContain("Following live")
    );
    const arriving = await screen.findByRole("list", {
      name: "What finished"
    });
    await waitFor(() => expect(arriving.textContent).toContain("implement"));
    expect(arriving.textContent).not.toContain("the draft");
  });

  it("proves(a-v3-stream-closes-only-when-every-event-has-arrived): keeps the stream open until the applied events match the run cursor", async () => {
    const feed = new FakeRunEventFeed();
    const ended = v3Run({
      state: "COMPLETED",
      terminal_hash: terminalHash,
      current_node_id: "review",
      latest_event_cursor: eventCursor(2),
      node_rail: [
        { node_id: "implement", state: "succeeded", attempt: null },
        { node_id: "review", state: "succeeded", attempt: null }
      ]
    });
    const getRun = vi.fn().mockResolvedValueOnce(v3Run()).mockResolvedValue(ended);
    const cockpitApi = api(v3Run(), { getRun, openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await completedEvent("implement", "the draft", 1)));

    const arriving = await screen.findByRole("list", { name: "What finished" });
    await waitFor(() => expect(arriving.textContent).toContain("implement"));
    expect(within(arriving).getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Following live");
    expect(feed.close).not.toHaveBeenCalled();

    feed.handlers?.event(JSON.stringify(await completedEvent("review", "looks good", 2)));

    await waitFor(() => expect(arriving.textContent).toContain("review"));
    expect(within(arriving).getAllByRole("listitem")).toHaveLength(2);
    await waitFor(() =>
      expect(screen.getByLabelText("Where this run stands").textContent).toContain("Ended")
    );
    expect(feed.close).toHaveBeenCalled();
  });

  it("shows the terminal hash once the run has ended", async () => {
    const cockpitApi = api(
      v3Run({ state: "COMPLETED", terminal_hash: terminalHash, current_node_id: "review" })
    );

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect((await screen.findByRole("button", { name: "Terminal hash" })).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: "Terminal hash" }).getAttribute("title")).toBeNull();
    expect(screen.queryByText(terminalHash)).toBeNull();
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Done");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("asks for the published revision so it can draw the edges, and opens the stream it can now read", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    await screen.findByRole("region", { name: "Workflow" });
    expect(cockpitApi.getWorkflowRevision).toHaveBeenCalledWith(digest);
    expect(feed.open).toHaveBeenCalledTimes(1);
  });

  it("says it is looking while the published graph is still arriving", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getWorkflowRevision: vi.fn(() => new Promise<ReturnType<typeof v3Revision>>(() => undefined))
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    // A V3 graph always declares a name once read; while it is still arriving
    // the title says that honestly instead of falling back to the raw run id.
    await screen.findByRole("heading", { level: 1, name: "Looking…" });
    expect(screen.getByRole("status").textContent).toBe("Looking…");
    expect(screen.queryByRole("region", { name: "Workflow" })).toBeNull();
    // The breadcrumb mirrors the same title truth as the h1 (#506): one
    // owner, not a second guess that would call this "Unnamed" instead.
    const trail = screen.getByRole("navigation", { name: "Where you are" });
    expect(within(trail).getByText("Looking…").isConnected).toBe(true);
  });

  it("names a graph that could not be read instead of inventing a line from the rail", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getWorkflowRevision: vi.fn(async () => {
            throw new Error("store asleep");
          })
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect((await screen.findByText("The graph could not be read")).isConnected).toBe(true);
    expect(screen.getByText("store asleep").isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: "Workflow" })).toBeNull();
    expect(screen.getByRole("button", { name: /implement/ }).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: /review/ }).isConnected).toBe(true);
    // A graph that could not be read still has no name to show; the title
    // names that state rather than falling back to the raw run id.
    expect(
      screen.getByRole("heading", { level: 1, name: "Workflow unavailable" }).isConnected
    ).toBe(true);
    // The breadcrumb mirrors the same title truth as the h1 (#506): one
    // owner, not a second guess that would call this "Unnamed" instead.
    const trail = screen.getByRole("navigation", { name: "Where you are" });
    expect(within(trail).getByText("Workflow unavailable").isConnected).toBe(true);
  });
});

describe("a started run shows the working node live", () => {
  it("proves(a-started-run-shows-the-working-node-live): the working node is live work, the stream's three truths stay distinct, and the page names the log that is not on this door", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    const graph = await screen.findByRole("region", { name: "Workflow" });
    const working = within(graph).getByRole("button", { name: "review — Working" });
    expect(working.getAttribute("data-live")).toBe("true");
    expect(working.classList.contains("live-work")).toBe(true);
    expect(within(graph).getByRole("button", { name: "implement — Done" }).getAttribute("data-live")).toBeNull();

    const standing = screen.getByLabelText("Where this run stands");
    expect(standing.textContent).toContain("Connecting");
    const now = screen.getByRole("region", { name: "Now" });
    expect(now.textContent).toContain("review");
    expect(now.textContent).toContain("Process log stays in the lease.");
    expect(now.textContent).not.toContain("No events yet.");
    expect(screen.queryByRole("progressbar")).toBeNull();

    feed.handlers?.opened();
    await waitFor(() => expect(standing.textContent).toContain("Following live"));
    expect(now.textContent).toContain("No events yet.");

    feed.handlers?.event(JSON.stringify(await completedEvent("implement", "the draft", 1)));

    await waitFor(() => expect(now.textContent).toContain("AGENT COMPLETED"));
    expect(now.textContent).toContain("implement");
    expect(now.textContent).not.toContain("the draft");
    expect(now.textContent).toContain("Process log stays in the lease.");
    const finished = await screen.findByRole("list", { name: "What finished" });
    expect(finished.textContent).toContain("implement");
    expect(finished.textContent).not.toContain("the draft");
    expect(working.getAttribute("data-live")).toBe("true");
  });

  it("proves(a-started-run-shows-the-working-node-live): a failed stream is Stopped with the server's problem, not Following live", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { openRunEvents: feed.open }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(
      JSON.stringify({
        event: "STREAM_FAILED",
        problem: {
          type: "urn:atelier2:problem:v1:durable-state-corrupt",
          title: "Durable state is corrupt",
          status: 500,
          detail: "Stop mutation and inspect the durable store."
        }
      })
    );

    const notice = await screen.findByRole("alert");
    expect(notice.textContent).toContain("Durable state is corrupt");
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Stopped");
    expect(screen.getByLabelText("Where this run stands").textContent).not.toContain("Following live");
    expect(screen.getByText("Process log stays in the lease.").isConnected).toBe(true);

    // A STREAM_FAILED frame closes the stream for good; nothing but this
    // named, in-place affordance reopens it (#506).
    const retry = screen.getByRole("button", { name: "Retry" });
    await fireEvent.click(retry);
    await waitFor(() => expect(feed.open).toHaveBeenCalledTimes(2));
    feed.handlers?.opened();
    await waitFor(() =>
      expect(screen.getByLabelText("Where this run stands").textContent).toContain("Following live")
    );
  });

  it("proves(a-started-run-shows-the-working-node-live): a corrupt event is named as itself", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { openRunEvents: feed.open }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event("not-json");

    expect((await screen.findByText("Event invalid")).isConnected).toBe(true);
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Stopped");
    expect(screen.getByLabelText("Where this run stands").textContent).not.toContain("Following live");
  });

  it("does not keep the live card on a finished run", async () => {
    render(App, {
      props: {
        cockpitApi: api(
          v3Run({
            state: "COMPLETED",
            terminal_hash: terminalHash,
            current_node_id: "review",
            node_rail: [
              { node_id: "implement", state: "succeeded", attempt: null },
              { node_id: "review", state: "succeeded", attempt: null }
            ]
          })
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    expect(screen.queryByRole("region", { name: "Now" })).toBeNull();
    expect(screen.queryByText("Process log stays in the lease.")).toBeNull();
    expect(document.querySelector("[data-live='true']")).toBeNull();
  });
});

describe("a version 3 run that stops for a person", () => {
  const answer = '"approved, with the second paragraph rewritten"';

  function waitRevision() {
    const revision = v3Revision();
    return {
      ...revision,
      graph: {
        ...revision.graph,
        name: "A person approves last",
        node_previews: [
          revision.graph.node_previews[0]!,
          {
            id: "approve",
            kind: "wait" as const,
            role: null,
            instruction_start: null,
            depends_on: ["implement"]
          }
        ]
      }
    };
  }

  function waitingRun(): RunV3 {
    return v3Run({
      run_id: "v3/a-person-approves",
      state: "WAITING_INPUT",
      current_node_id: "approve",
      node_rail: [
        { node_id: "implement", state: "succeeded", attempt: null },
        { node_id: "approve", state: "needs_you", attempt: null }
      ]
    });
  }

  function answeredRun(): RunV3 {
    return v3Run({
      run_id: "v3/a-person-approves",
      state: "COMPLETED",
      current_node_id: "approve",
      node_rail: [
        { node_id: "implement", state: "succeeded", attempt: null },
        { node_id: "approve", state: "succeeded", attempt: null }
      ],
      terminal_hash: terminalHash
    });
  }

  async function waitAnsweredEvent(sequence: number) {
    return {
      workflow_format_version: 3,
      cursor: `event1.cnVu.${sequence}`,
      sequence,
      public_run_reference: publicReference,
      workflow_revision_hash: digest,
      node_id: "approve",
      node_execution_id: "b".repeat(64),
      event_hash: "c".repeat(64),
      node_rail: [{ node_id: "approve", state: "succeeded", attempt: null }],
      event: "WAIT_ANSWERED",
      answer_base64: btoa(answer),
      answer_hash: [
        ...new Uint8Array(
          await crypto.subtle.digest("SHA-256", new TextEncoder().encode(answer))
        )
      ]
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("")
    };
  }

  function waitNodeDetail(job: string | null = "Approve this, or name the blocking defect.") {
    return {
      run_id: "v3/a-person-approves",
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

  it("proves(a-v3-line-stops-for-a-person-and-their-answer-carries-it-on): draws the node that owes a person a move as the one needing them", async () => {
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async () => waitNodeDetail() as never)
    });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    const graph = await screen.findByRole("region", { name: "Workflow" });
    expect(within(graph).getByRole("button", { name: "approve — Needs you" }).isConnected).toBe(
      true
    );
    expect(within(graph).getByRole("button", { name: "implement — Done" }).isConnected).toBe(true);
    expect(screen.getByText(/not yet/i).isConnected).toBe(true);
  });

  it("proves(a-v3-line-stops-for-a-person-and-their-answer-carries-it-on): carries the page on when the answer arrives, without an answer of its own to settle", async () => {
    const feed = new FakeRunEventFeed();
    const journal = new MutationJournal(sessionStorage);
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValue(answeredRun());
    const cockpitApi = api(waitingRun(), {
      getRun,
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async () => waitNodeDetail() as never),
      openRunEvents: feed.open
    });

    render(App, { props: { cockpitApi, mutationJournal: journal } });
    await screen.findByRole("button", { name: "approve — Needs you" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await waitAnsweredEvent(1)));

    expect((await screen.findByRole("button", { name: "Terminal hash" })).isConnected).toBe(true);
    expect(screen.queryByText(terminalHash)).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "approve — Done" }).isConnected).toBe(true)
    );
    expect(await journal.entries()).toEqual([]);
    expect(screen.queryByText("Run unavailable")).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): shows the wait and sends the answer through the existing door", async () => {
    const journal = new MutationJournal(sessionStorage);
    const answer = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 200, value: answeredRun() };
    });
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async () => waitNodeDetail() as never),
      answer
    });

    render(App, { props: { cockpitApi, mutationJournal: journal } });

    expect(await screen.findByRole("heading", { name: "Answer needed" })).toBeTruthy();
    expect(
      await screen.findByText("Approve this, or name the blocking defect.")
    ).toBeTruthy();
    expect(screen.getByText("Wait approve")).toBeTruthy();
    await fireEvent.input(screen.getByLabelText("Answer"), {
      target: { value: '"approved, with the second paragraph rewritten"' }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Answer" }));

    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
    const mutation = answer.mock.calls[0]?.[0];
    const body = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(body).toEqual({
      workflow_revision_hash: digest,
      node_id: "approve",
      answer_base64: btoa('"approved, with the second paragraph rewritten"')
    });
    expect((await screen.findByRole("button", { name: "Terminal hash" })).isConnected).toBe(true);
    expect(screen.queryByText(terminalHash)).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names a refused answer on the card", async () => {
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async () => waitNodeDetail() as never),
      answer: vi.fn(async () => {
        throw new CockpitRequestError("The durable run is no longer waiting for this answer.", {
          type: "urn:atelier2:problem:v1:answer-state-conflict",
          title: "Answer state conflict",
          status: 409,
          detail: "The durable run is no longer waiting for this answer."
        }, true);
      })
    });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { name: "Answer needed" });
    await fireEvent.input(screen.getByLabelText("Answer"), { target: { value: "true" } });
    await fireEvent.click(screen.getByRole("button", { name: "Answer" }));

    const alert = await screen.findByRole("alert", { name: "Send failed" });
    expect(alert.textContent).toContain("The durable run is no longer waiting for this answer.");
    expect(screen.getByLabelText("Answer").isConnected).toBe(true);
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names an absent question instead of the bare node id", async () => {
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async () => waitNodeDetail(null) as never)
    });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(await screen.findByText("This wait node carries no question.")).toBeTruthy();
    expect(screen.queryByText("Approve this, or name the blocking defect.")).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names a damaged question instead of an honest absence", async () => {
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async () => ({
        ...waitNodeDetail(),
        job_base64: "////"
      }) as never)
    });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(await screen.findByText("The wait question could not be read")).toBeTruthy();
    expect(screen.queryByText("This wait node carries no question.")).toBeNull();
    const card = screen.getByRole("region", { name: "Answer needed" });
    expect(within(card).queryByText("Looking…")).toBeNull();
  });
});


async function failedEvent(nodeId: string, reason: string, sequence: number) {
  return {
    workflow_format_version: 3,
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: nodeId,
    node_execution_id: "b".repeat(64),
    event_hash: "c".repeat(64),
    node_rail: [{ node_id: nodeId, state: "failed" as const, attempt: null }],
    event: "AGENT_FAILED",
    failure_code: "OUTPUT_SCHEMA_REFUSED",
    reason,
    attempt_id: "e".repeat(64),
    attempt_ordinal: 1
  };
}

async function completedEvent(nodeId: string, output: string, sequence: number) {
  const encoded = btoa(output);
  // Named apart from the imported revision digest on purpose: one shadowed the
  // other once, and the strict decoder refused the event rather than quietly
  // reading an ArrayBuffer as a hash.
  const outputDigest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(output)
  );
  return {
    workflow_format_version: 3,
    cursor: `event1.cnVu.${sequence}`,
    sequence,
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    node_id: nodeId,
    node_execution_id: "b".repeat(64),
    event_hash: "c".repeat(64),
    node_rail: [{ node_id: nodeId, state: "succeeded", attempt: null }],
    event: "AGENT_COMPLETED",
    output_base64: encoded,
    output_hash: [...new Uint8Array(outputDigest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join(""),
    attempt_id: "e".repeat(64),
    attempt_ordinal: 1
  };
}

describe("a failed node on the run page", () => {
  it("proves(a-failed-run-page-does-not-pose-as-working): a dead run is Failed, not Working, and empty facts do not say yet", async () => {
    const getNodeDetail = vi.fn(async () =>
      ({
        run_id: "v3/two-agents",
        public_run_reference: publicReference,
        node_id: "implement",
        state: "failed",
        job_base64: btoa("Write three German sentences about code review."),
        job_hash: "e".repeat(64),
        answer: null,
        provenance: null,
        refusal: "output-schema-refused: instance-not-json: Expecting value",
        started_at: "2026-08-18T15:00:00Z",
        ended_at: "2026-08-18T15:00:12Z"
      }) as never
    );
    render(App, {
      props: {
        cockpitApi: api(
          v3Run({
            state: "FAILED",
            current_node_id: "implement",
            terminal_hash: terminalHash,
            ended_at: "2026-08-18T15:00:12Z",
            node_rail: [
              { node_id: "implement", state: "failed", attempt: { ordinal: 1, state: "FAILED" } },
              { node_id: "review", state: "queued", attempt: null }
            ]
          }),
          { getNodeDetail }
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    expect(screen.getByRole("button", { name: "implement — Failed" }).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: /Working/ })).toBeNull();
    expect(screen.queryByText("Working")).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "implement — Failed" }));

    await screen.findByText("Nothing written.");
    await screen.findByText("No receipt.");
    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Usage").closest("p")?.textContent).toMatch(/not recorded$/);
    expect(within(who).getByText("Resolved model").closest("p")?.textContent).toMatch(
      /not recorded$/
    );
    await screen.findByText("12 s");
    expect(screen.queryByText(/yet/)).toBeNull();
    expect(screen.queryByText("a moment")).toBeNull();
  });

  it("proves(a-failed-node-shows-the-stored-reason-on-the-run-page): shows the stored reason at the failed node without a click", async () => {
    const feed = new FakeRunEventFeed();
    const reason = "output-schema-refused: instance-not-json: Expecting value";
    const cockpitApi = api(
      v3Run({
        state: "FAILED",
        current_node_id: "implement",
        node_rail: [
          { node_id: "implement", state: "failed", attempt: null },
          { node_id: "review", state: "queued", attempt: null }
        ]
      }),
      { openRunEvents: feed.open }
    );

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await failedEvent("implement", reason, 1)));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(reason);
    expect(screen.getByRole("button", { name: "implement — Failed" }).textContent).toContain(
      reason
    );
    expect(screen.queryByText("Asked")).toBeNull();
  });
});

describe("the click into a node", () => {
  // Both values stand well over the 120 characters at which the timeline cuts
  // its preview, because the sentence these tests carry says the panel shows the
  // job and the answer WHOLE. Under a shorter value a truncating panel passes
  // every assertion here, so the value is the proof: shorten either one and the
  // clause stops being tested.
  const asked =
    "Judge the draft you were handed, sentence by sentence, and say plainly which of them you would send back to its author, and for what reason.";
  const wrote =
    "Ein gutes Code-Review schuetzt vor fehlerhaftem Code. Es liest zuerst die Absicht und danach die Zeilen. Wer nur die Zeilen liest, findet Tippfehler und keine Denkfehler.";

  function nodeDetail(overrides: Record<string, unknown> = {}) {
    return {
      run_id: "v3/two-agents",
      public_run_reference: publicReference,
      node_id: "implement",
      state: "succeeded",
      job_base64: btoa(asked),
      job_hash: "e".repeat(64),
      answer: { value_base64: btoa(wrote), value_hash: "f".repeat(64) },
      provenance: {
        role: "builder",
        provider_id: "anthropic",
        model: "sonnet",
        executor_revision: "headless-print-json/v1",
        executor_operational_identity: "headless-print-json/v1",
        auth_mode: "subscription",
        profile_id: "operator-subscription",
        agent_configuration_revision_hash: "a".repeat(64),
        request_hash: "b".repeat(64),
        receipt_hash: "c".repeat(64)
      },
      refusal: null,
      ...overrides
    };
  }

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): asks the server for that node and shows what it was asked, wrote and who ran it", async () => {
    const getNodeDetail = vi.fn(async () => nodeDetail() as never);
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    expect(getNodeDetail).toHaveBeenCalledWith(publicReference, "implement");
    // The whole answer, not a preview: an operator asked to see the log.
    await screen.findByText(asked);
    await screen.findByText(wrote);
    const who = await screen.findByRole("region", { name: "Who" });
    expect(who.textContent).toMatch(/builder · anthropic/);
    expect(within(who).getByText("sonnet").isConnected).toBe(true);
    expect(screen.getByRole("button", { name: "Prompt hash" }).textContent).toBe("Prompt hash");
    expect(screen.getByRole("button", { name: "Output hash" }).textContent).toBe("Output hash");
    expect(screen.getByRole("button", { name: "Receipt hash" }).textContent).toBe("Receipt hash");
    expect(screen.queryByText("e".repeat(64))).toBeNull();
    expect(screen.queryByText("f".repeat(64))).toBeNull();
  });

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): says usage is not recorded instead of leaving the question open", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Usage").closest("p")?.textContent).toMatch(/not recorded$/);
    expect(screen.queryByText(/not recorded yet/)).toBeNull();
  });

  it("proves(a-node-carries-how-long-it-ran): shows the recorded duration on a node that ran", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getNodeDetail: vi.fn(async () =>
            nodeDetail({
              started_at: "2026-08-18T15:00:00Z",
              ended_at: "2026-08-18T15:05:00Z"
            }) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    await screen.findByText("Duration");
    await screen.findByText("5 min");
    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Usage").closest("p")?.textContent).toMatch(/not recorded$/);
    expect(screen.queryByText(/not recorded yet/)).toBeNull();
  });

  it("proves(a-stopped-node-says-so-and-a-waiting-one-does-not): shows the refusal that stops the run, in the words of the owner that refused", async () => {
    const stopped = nodeDetail({
      node_id: "review",
      state: "working",
      job_base64: null,
      job_hash: null,
      answer: null,
      provenance: null,
      refusal:
        "node 'implement' produced an output its own schema refuses: instance-not-json: Expecting value"
    });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => stopped as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await fireEvent.click(await screen.findByRole("button", { name: /review/ }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Stopped here");
    expect(alert.textContent).toContain("instance-not-json");
    expect(alert.textContent).toContain("implement");
  });

  it("proves(a-stopped-node-says-so-and-a-waiting-one-does-not): shows a node whose work has not arrived as waiting, not as refused", async () => {
    const waiting = nodeDetail({
      node_id: "review",
      state: "queued",
      job_base64: null,
      job_hash: null,
      answer: null,
      provenance: null,
      refusal: null
    });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => waiting as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await fireEvent.click(await screen.findByRole("button", { name: /review/ }));

    await screen.findByText(/Waiting for the work before it/);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("proves(a-stopped-node-says-so-and-a-waiting-one-does-not): shows a store that disagrees with itself as a problem, not as a tidy refusal", async () => {
    const getNodeDetail = vi.fn(async () => {
      throw new Error("Durable state is corrupt");
    });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: getNodeDetail as never }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    await screen.findByText("This node could not be read");
    expect(screen.queryByRole("alert")?.textContent ?? "").not.toContain("Stopped here");
  });

  it("proves(a-started-run-shows-the-working-node-live): a click into the working node is live work and names the log that is not here", async () => {
    render(App, {
      props: {
        cockpitApi: api(
          v3Run(),
          {
            getNodeDetail: vi.fn(async () =>
              nodeDetail({
                node_id: "review",
                state: "working",
                answer: null,
                provenance: null
              }) as never
            )
          }
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("button", { name: "review — Working" });
    await fireEvent.click(screen.getByRole("button", { name: "review — Working" }));

    await screen.findByText("Nothing written yet.");
    expect(document.querySelector(".node-panel.live-work")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Now" }).textContent).toContain(
      "Process log stays in the lease."
    );
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("proves(a-run-page-speaks-prompt-and-output): labels the job Prompt and the value Output", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    await screen.findByRole("heading", { name: "Prompt" });
    await screen.findByRole("heading", { name: "Output" });
    expect(screen.queryByText("Asked")).toBeNull();
    expect(screen.queryByText("Answered")).toBeNull();
  });

  it("proves(a-run-page-labels-the-declared-model): labels the receipt model as the declared configuration model and says a resolved model is not recorded", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Declared model").isConnected).toBe(true);
    expect(within(who).getByText("sonnet").isConnected).toBe(true);
    const resolved = within(who).getByText("Resolved model").closest("p");
    expect(resolved?.textContent).toMatch(/not recorded$/);
    expect(resolved?.textContent).not.toContain("sonnet");
    await fireEvent.click(within(who).getByRole("button", { name: "Why resolved model is missing" }));
    expect(
      within(who).getByText(/No receipt records a provider-resolved model/).isConnected
    ).toBe(true);
  });

  it("proves(a-run-page-labels-the-declared-model): a working node without a receipt does not invent a declared model and still names the unrecorded resolved one", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getNodeDetail: vi.fn(async () =>
            nodeDetail({
              node_id: "review",
              state: "working",
              answer: null,
              provenance: null
            }) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await fireEvent.click(await screen.findByRole("button", { name: "review — Working" }));

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("No receipt yet.").isConnected).toBe(true);
    expect(within(who).queryByText("Declared model")).toBeNull();
    expect(within(who).queryByText("sonnet")).toBeNull();
    expect(within(who).getByText("Resolved model").closest("p")?.textContent).toMatch(
      /not recorded yet$/
    );
  });
});

describe("the run page speaking the target words", () => {
  it("proves(a-run-page-hash-is-a-named-proof-anchor): a hash leads with its name and a click copies it", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(App, {
      props: { cockpitApi: api(v3Run()), mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    expect(screen.queryByText(configurationHash)).toBeNull();
    const trigger = screen.getByRole("button", { name: "Run configuration" });
    expect(trigger.getAttribute("title")).toBeNull();
    expect(trigger.textContent).not.toContain(configurationHash);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    await fireEvent.click(trigger);
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(configurationHash);
    expect(screen.getByText(configurationHash).isConnected).toBe(true);
    await waitFor(() => expect(screen.getByText("Copied").isConnected).toBe(true));
  });

  it("proves(a-run-page-does-not-repeat-node-outputs-as-a-timeline): names the finished node without pasting its output or saying As it happened", async () => {
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { openRunEvents: feed.open }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await completedEvent("implement", "schreiben", 1)));

    const arriving = await screen.findByRole("list", { name: "What finished" });
    await waitFor(() => expect(arriving.textContent).toContain("implement"));
    expect(arriving.textContent).toContain("Done");
    expect(arriving.textContent).not.toContain("schreiben");
    expect(arriving.textContent).not.toMatch(/Doneschreiben/);
    expect(screen.queryByText("As it happened")).toBeNull();
  });

  it("proves(a-run-page-leads-with-the-workflow-name): the name is the title and the run id is a proof anchor beside it", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(App, {
      props: { cockpitApi: api(v3Run()), mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(
      (await screen.findByRole("heading", { level: 1, name: "Two agents in a line" })).isConnected
    ).toBe(true);
    expect(screen.queryByRole("heading", { level: 1, name: "Run v3/two-agents" })).toBeNull();
    const identity = screen.getByRole("button", { name: "Run id" });
    expect(identity.textContent).not.toContain("v3/two-agents");
    await fireEvent.click(identity);
    expect(writeText).toHaveBeenCalledWith("v3/two-agents");
    await waitFor(() => expect(screen.getByText("Copied").isConnected).toBe(true));
  });
});
