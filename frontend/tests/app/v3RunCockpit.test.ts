import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { CockpitRequestError, type CockpitApi, type RunV3 } from "../../src/api/client";
import { shortFingerprint } from "../../src/lib/fingerprint";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { runHeaderCopy } from "../../src/lib/runPages";
import { cancelReasonSentence, runPageCopy } from "../../src/lib/runPageCopy";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { cancellableBlock, notCancellableBlock } from "../support/runV3";
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
      wait_answer_schemas: [],
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
    cancellation: cancellableBlock(),
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

/** Opens a node and moves to one of its tabs, the way a reader does. */
async function openNodeTab(nodeName: RegExp | string, tab: string): Promise<void> {
  await fireEvent.click(await screen.findByRole("button", { name: nodeName }));
  await fireEvent.click(await screen.findByRole("tab", { name: tab }));
}

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
});

afterEach(() => cleanup());

describe("a version 3 run in the cockpit", () => {
  it("proves(a-v3-run-is-visible-in-the-cockpit): shows the line, which node is running, and no proof plumbing on the way", async () => {
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
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Running");
    // The main surface carries no fingerprints and no "not yet" placeholder:
    // every proof lives one click away, in the node's Evidence tab (operator
    // ruling 23.08.).
    expect(screen.queryByText(/not yet/i)).toBeNull();
    expect(screen.queryByText(configurationHash)).toBeNull();
    expect(screen.queryByRole("group", { name: runPageCopy.runConfiguration })).toBeNull();
    expect(screen.queryByRole("group", { name: runPageCopy.terminalHash })).toBeNull();
    // A loaded run is not a failed one: the page must not offer to fetch it again
    // beneath the answer it already has.
    expect(screen.queryByRole("button", { name: runPageCopy.readAgain })).toBeNull();
  });

  it("proves(a-run-carries-when-it-started-and-ended): shows the run's exact facts in the same line as its state, honestly omitting a timestamp that has not arrived and a duration its own state sentence already carries, with no reveal to find them behind", async () => {
    render(App, {
      props: { cockpitApi: api(v3Run()), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    const standing = screen.getByLabelText("Where this run stands");
    expect(standing.textContent).toContain("Running");
    expect(standing.textContent).toMatch(/for \d/);
    expect(standing.textContent).toMatch(/started \d/);
    // A run still going never repeats its own "for" reading as a second,
    // separately labelled duration fact (Zeiten-Hierarchie, operator ruling
    // 23.08.).
    expect(standing.textContent).not.toContain("duration");
    expect(standing.textContent).not.toContain("ended");
    expect(screen.queryByText("Exact time")).toBeNull();
  });

  it("proves(a-done-run-reads-the-time-since-it-landed): a finished run's relative reading is how long ago it ended, never how long it took to run", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-08-18T17:00:12Z"));
    try {
      render(App, {
        props: {
          cockpitApi: api(
            v3Run({
              state: "COMPLETED",
              node_rail: [
                { node_id: "implement", state: "succeeded", attempt: null },
                { node_id: "review", state: "succeeded", attempt: null }
              ],
              ended_at: "2026-08-18T15:00:12Z"
            })
          ),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      });

      await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

      const standing = screen.getByLabelText("Where this run stands");
      expect(standing.textContent).toContain("Done");
      // Two hours passed between the run ending and now -- not the twelve
      // seconds the run itself took to complete.
      expect(standing.textContent).toMatch(/2 h ago/);
      expect(standing.textContent).toMatch(/started .* · ended .* · duration 12 s/);
    } finally {
      vi.useRealTimers();
    }
  });

  it("proves(a-chain-run-is-watched-while-it-runs): moves the node that finished to Done on the one picture of the run", async () => {
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

    const graph = await screen.findByRole("region", { name: "Workflow" });
    await waitFor(() =>
      expect(within(graph).getByRole("button", { name: "implement — Done" }).isConnected).toBe(true)
    );
    // The graph is the one truth about where the run stands; the finished
    // node's output is not pasted beside it.
    expect(document.body.textContent).not.toContain("the draft");
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

    const graph = await screen.findByRole("region", { name: "Workflow" });
    await waitFor(() =>
      expect(within(graph).getByRole("button", { name: "implement — Done" }).isConnected).toBe(true)
    );
    expect(feed.close).not.toHaveBeenCalled();

    feed.handlers?.event(JSON.stringify(await completedEvent("review", "looks good", 2)));

    await waitFor(() => expect(feed.close).toHaveBeenCalled());
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Done");
  });

  it("keeps the terminal fingerprint out of the main surface and inside the node's evidence", async () => {
    const cockpitApi = api(
      v3Run({ state: "COMPLETED", terminal_hash: terminalHash, current_node_id: "review" }),
      { getNodeDetail: vi.fn(async () => finishedNodeDetail() as never) }
    );

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Done");
    expect(screen.queryByRole("group", { name: runPageCopy.terminalHash })).toBeNull();

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const terminal = await screen.findByRole("group", { name: runPageCopy.terminalHash });
    expect(within(terminal).getByText(shortFingerprint(terminalHash)).isConnected).toBe(true);
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
  });

  it("leads back to the Board without repeating the page's own title", async () => {
    render(App, {
      props: { cockpitApi: api(v3Run()), mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    const back = screen.getByRole("navigation", { name: "Where you are" });
    expect(within(back).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "←Board"
    ]);
    expect(within(back).queryByText("Two agents in a line")).toBeNull();
  });
});

describe("a started run shows the working node live", () => {
  it("proves(a-started-run-shows-the-working-node-live): the working node is live work, and the log that is not here is named where a log would live", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), {
      openRunEvents: feed.open,
      getNodeDetail: vi.fn(async () => workingNodeDetail() as never)
    });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    const graph = await screen.findByRole("region", { name: "Workflow" });
    const working = within(graph).getByRole("button", { name: "review — Working" });
    expect(working.getAttribute("data-live")).toBe("true");
    expect(within(graph).getByRole("button", { name: "implement — Done" }).getAttribute("data-live")).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
    // Connecting is ordinary loading, not a problem worth a line of its own.
    expect(screen.queryByText(runPageCopy.streamStale)).toBeNull();

    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await completedEvent("implement", "the draft", 1)));

    await openNodeTab("review — Working", runPageCopy.tabLog);
    expect(screen.getByText(runPageCopy.processLogInLease).isConnected).toBe(true);
    expect(screen.getByText(runPageCopy.logAbsent).isConnected).toBe(true);
  });

  it("proves(a-started-run-shows-the-working-node-live): a failed stream says the page is not following, and reading again heals it", async () => {
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
    expect(screen.getByText(runPageCopy.streamStale).isConnected).toBe(true);

    // A stream that stopped is never a dead end: the one act that can heal it
    // stands right beside the words that say it stopped (operator, 23.08.).
    const readAgain = screen.getByRole("button", { name: runPageCopy.readAgain });
    await fireEvent.click(readAgain);
    await waitFor(() => expect(feed.open).toHaveBeenCalledTimes(2));
    feed.handlers?.opened();
    await waitFor(() => expect(screen.queryByText(runPageCopy.streamStale)).toBeNull());
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
    expect(screen.getByText(runPageCopy.streamStale).isConnected).toBe(true);
  });

  it("does not keep a live mark on a finished run", async () => {
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
    expect(document.querySelector("[data-live='true']")).toBeNull();
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Done");
  });
});

describe("a version 3 run that stops for a person", () => {
  const answer = '"approved, with the second paragraph rewritten"';
  const question = "Approve this, or name the blocking defect.";
  const earlierResult = "Three German sentences about code review.";

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
      cancellation: notCancellableBlock("waiting-for-you"),
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
      cancellation: notCancellableBlock("already-ended"),
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

  function waitNodeDetail(job: string | null = question) {
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

  function earlierNodeDetail() {
    return {
      run_id: "v3/a-person-approves",
      public_run_reference: publicReference,
      node_id: "implement",
      state: "succeeded",
      job_base64: btoa("Write three German sentences about code review."),
      job_hash: "e".repeat(64),
      answer: { value_base64: btoa(earlierResult), value_hash: "f".repeat(64) },
      provenance: null,
      refusal: null
    };
  }

  /** Answers the wait node with the question, and every other node with its result. */
  function waitingApi(overrides: Partial<CockpitApi> = {}, job: string | null = question) {
    return api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async (_reference: string, nodeId: string) =>
        (nodeId === "approve" ? waitNodeDetail(job) : earlierNodeDetail()) as never
      ),
      ...overrides
    });
  }

  it("proves(a-v3-line-stops-for-a-person-and-their-answer-carries-it-on): draws the node that owes a person a move as the one needing them", async () => {
    render(App, {
      props: { cockpitApi: waitingApi(), mutationJournal: new MutationJournal(sessionStorage) }
    });

    const graph = await screen.findByRole("region", { name: "Workflow" });
    expect(within(graph).getByRole("button", { name: "approve — Needs you" }).isConnected).toBe(
      true
    );
    expect(within(graph).getByRole("button", { name: "implement — Done" }).isConnected).toBe(true);
    expect(screen.getByLabelText("Where this run stands").textContent).toContain(
      "Waiting for you"
    );
  });

  it("proves(a-v3-line-stops-for-a-person-and-their-answer-carries-it-on): carries the page on when the answer arrives, without an answer of its own to settle", async () => {
    const feed = new FakeRunEventFeed();
    const journal = new MutationJournal(sessionStorage);
    const getRun = vi
      .fn()
      .mockResolvedValueOnce(waitingRun())
      .mockResolvedValue(answeredRun());
    const cockpitApi = waitingApi({ getRun, openRunEvents: feed.open });

    render(App, { props: { cockpitApi, mutationJournal: journal } });
    await screen.findByRole("button", { name: "approve — Needs you" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await waitAnsweredEvent(1)));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "approve — Done" }).isConnected).toBe(true)
    );
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Done");
    expect(await journal.entries()).toEqual([]);
    expect(screen.queryByText("Run unavailable")).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): leads with the question and the earlier result it is about", async () => {
    render(App, {
      props: { cockpitApi: waitingApi(), mutationJournal: new MutationJournal(sessionStorage) }
    });

    // The question is the title of the card. "WAIT approve" tells a person
    // nothing they can act on (operator, 23.08.).
    expect(await screen.findByRole("heading", { name: question })).toBeTruthy();
    expect(screen.queryByText("Wait approve")).toBeNull();

    const context = await screen.findByRole("region", { name: runPageCopy.answerContext });
    await waitFor(() =>
      expect(within(context).getByText(earlierResult).isConnected).toBe(true)
    );
    expect(within(context).getByRole("article", { name: "implement" }).isConnected).toBe(true);
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): sends plain words as the JSON string the schema expects", async () => {
    const journal = new MutationJournal(sessionStorage);
    const answerCall = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 200, value: answeredRun() };
    });

    render(App, {
      props: {
        cockpitApi: waitingApi({ answer: answerCall }),
        mutationJournal: journal
      }
    });

    await screen.findByRole("heading", { name: question });
    await fireEvent.input(screen.getByLabelText(runPageCopy.answerLabel), {
      target: { value: "approved, with the second paragraph rewritten" }
    });
    await fireEvent.click(screen.getByRole("button", { name: runPageCopy.answerSubmit }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body).toEqual({
      workflow_revision_hash: digest,
      node_id: "approve",
      answer_base64: btoa(answer)
    });
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): passes an answer already written as JSON through untouched", async () => {
    const answerCall = vi.fn(async (mutation: { body_base64: string }) => {
      void mutation;
      return { status: 200, value: answeredRun() };
    });

    render(App, {
      props: {
        cockpitApi: waitingApi({ answer: answerCall }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: question });
    await fireEvent.input(screen.getByLabelText(runPageCopy.answerLabel), {
      target: { value: '{"verdict":"green"}' }
    });
    await fireEvent.click(screen.getByRole("button", { name: runPageCopy.answerSubmit }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body.answer_base64).toBe(btoa('{"verdict":"green"}'));
  });

  /** The published answer schema of #553's decision-button graphs, everything but its classification. */
  function decisionSchema(kind: "boolean" | "enum" | "free", values: string[] | null = null) {
    const revision = waitRevision();
    return {
      ...revision,
      graph: {
        ...revision.graph,
        wait_answer_schemas: [
          {
            node_id: "approve",
            schema: { ref: "decision", revision: "e".repeat(64) },
            kind,
            values
          }
        ]
      }
    };
  }

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): a boolean schema renders two decision buttons, sends the exact click, and confirms it", async () => {
    let resolveAnswer: (result: { status: 200; value: RunV3 }) => void = () => {};
    const answerCall = vi.fn(
      (mutation: { body_base64: string }) =>
        new Promise<{ status: 200; value: RunV3 }>((resolve) => {
          void mutation;
          resolveAnswer = resolve;
        })
    );
    render(App, {
      props: {
        cockpitApi: waitingApi({
          answer: answerCall,
          getWorkflowRevision: vi.fn(async () => decisionSchema("boolean"))
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: question });
    expect(screen.queryByRole("textbox")).toBeNull();

    await fireEvent.click(await screen.findByRole("button", { name: runPageCopy.answerYes }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body.answer_base64).toBe(btoa("true"));
    await screen.findByText(`${runPageCopy.answeredPrefix} ${runPageCopy.answerYes}`);
    resolveAnswer({ status: 200, value: answeredRun() });
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): an enum schema renders one button per value and sends its exact JSON", async () => {
    let resolveAnswer: (result: { status: 200; value: RunV3 }) => void = () => {};
    const answerCall = vi.fn(
      (mutation: { body_base64: string }) =>
        new Promise<{ status: 200; value: RunV3 }>((resolve) => {
          void mutation;
          resolveAnswer = resolve;
        })
    );
    render(App, {
      props: {
        cockpitApi: waitingApi({
          answer: answerCall,
          getWorkflowRevision: vi.fn(async () =>
            decisionSchema("enum", ['"approve"', '"revise"'])
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { name: question });
    expect(screen.queryByRole("textbox")).toBeNull();

    await fireEvent.click(await screen.findByRole("button", { name: "revise" }));

    await waitFor(() => expect(answerCall).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(answerCall.mock.calls[0]?.[0]?.body_base64 ?? ""));
    expect(body.answer_base64).toBe(btoa('"revise"'));
    await screen.findByText(`${runPageCopy.answeredPrefix} revise`);
    resolveAnswer({ status: 200, value: answeredRun() });
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): keeps the free-form textarea for a schema this build has not classified", async () => {
    render(App, {
      props: { cockpitApi: waitingApi(), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { name: question });

    expect(screen.getByLabelText(runPageCopy.answerLabel).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: runPageCopy.answerYes })).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names a refused answer on the card", async () => {
    render(App, {
      props: {
        cockpitApi: waitingApi({
          answer: vi.fn(async () => {
            throw new CockpitRequestError(
              "The durable run is no longer waiting for this answer.",
              {
                type: "urn:atelier2:problem:v1:answer-state-conflict",
                title: "Answer state conflict",
                status: 409,
                detail: "The durable run is no longer waiting for this answer."
              },
              true
            );
          })
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: question });
    await fireEvent.input(screen.getByLabelText(runPageCopy.answerLabel), {
      target: { value: "yes" }
    });
    await fireEvent.click(screen.getByRole("button", { name: runPageCopy.answerSubmit }));

    const alert = await screen.findByRole("alert", { name: "Send failed" });
    expect(alert.textContent).toContain("The durable run is no longer waiting for this answer.");
    expect(screen.getByLabelText(runPageCopy.answerLabel).isConnected).toBe(true);
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names an absent question instead of the bare node id", async () => {
    render(App, {
      props: {
        cockpitApi: waitingApi({}, null),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(await screen.findByText(runPageCopy.questionMissing)).toBeTruthy();
    expect(screen.queryByText(question)).toBeNull();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names a damaged question instead of an honest absence", async () => {
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
      getNodeDetail: vi.fn(async (_reference: string, nodeId: string) =>
        (nodeId === "approve"
          ? { ...waitNodeDetail(), job_base64: "////" }
          : earlierNodeDetail()) as never
      )
    });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(await screen.findByText("The wait question could not be read")).toBeTruthy();
    expect(screen.queryByText(runPageCopy.questionMissing)).toBeNull();
    expect(screen.queryByText(runPageCopy.questionLooking)).toBeNull();
  });
});

describe("cancelling a version 3 run from the cockpit", () => {
  const cancel = runPageCopy.cancel;
  const targetNodeExecutionId = "d".repeat(64);

  beforeEach(() => {
    Object.defineProperties(HTMLDialogElement.prototype, {
      showModal: {
        configurable: true,
        value(this: HTMLDialogElement): void {
          this.open = true;
        }
      },
      close: {
        configurable: true,
        value(this: HTMLDialogElement): void {
          this.open = false;
        }
      }
    });
    sessionStorage.clear();
    window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
  });

  afterEach(() => cleanup());

  function overtakenError(): CockpitRequestError {
    return new CockpitRequestError(
      "The agent finished before this cancel reached it; its result stands and the run moved on.",
      {
        type: "urn:atelier2:problem:v1:run-cancellation-overtaken-by-success",
        title: "Run cancellation overtaken by success",
        status: 409,
        detail: "The agent finished before this cancel reached it; its result stands and the run moved on."
      },
      true
    );
  }

  async function openStagedDecision(): Promise<void> {
    await fireEvent.click(await screen.findByRole("button", { name: cancel.open }));
    expect(await screen.findByRole("heading", { name: cancel.question })).toBeTruthy();
  }

  it("proves(the-cockpit-cancels-a-running-v3-run): stages the decision, then stops the run on the one audited path with an idempotency-keyed command", async () => {
    const journal = new MutationJournal(sessionStorage);
    const cancelRun = vi.fn<CockpitApi["cancelRun"]>().mockResolvedValue({
      status: 202,
      value: v3Run({ cancellation: notCancellableBlock("already-cancelling") })
    });
    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal } });

    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.confirm }));

    await waitFor(() => expect(cancelRun).toHaveBeenCalledTimes(1));
    const sent = cancelRun.mock.calls[0]?.[0];
    expect(sent?.expected_node_execution_id).toBe(targetNodeExecutionId);
    expect((sent?.idempotency_key ?? "").length).toBeGreaterThan(0);

    expect(await screen.findByText(cancel.accepted)).toBeTruthy();
    const cancelEntries = (await journal.entries()).filter((entry) => entry.kind === "cancel");
    expect(cancelEntries).toHaveLength(1);
    // The staged control does not reappear while the run is stopping: no second cancel.
    expect(screen.queryByRole("button", { name: cancel.open })).toBeNull();
  });

  it("dismisses the staged decision without sending a cancel", async () => {
    const cancelRun = vi.fn();
    render(App, {
      props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.dismiss }));

    await waitFor(() => expect(screen.queryByRole("heading", { name: cancel.question })).toBeNull());
    expect(cancelRun).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: cancel.open })).toBeTruthy();
  });

  it("proves(a-run-that-finished-first-is-not-called-cancelled): tells the operator the run finished before the cancel, with no false cancelled and no retry", async () => {
    const journal = new MutationJournal(sessionStorage);
    const cancelRun = vi.fn(async () => {
      throw overtakenError();
    });
    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal } });

    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.confirm }));

    expect(
      await screen.findByText(/finished before this cancel reached it/)
    ).toBeTruthy();
    // The run moved on, so its standing must not read as cancelled.
    expect(screen.getByLabelText("Where this run stands").textContent).not.toContain("Cancelled");
    expect(screen.queryByText(cancel.accepted)).toBeNull();
    expect(screen.queryByRole("button", { name: cancel.retry })).toBeNull();
    expect((await journal.entries()).filter((entry) => entry.kind === "cancel")).toHaveLength(0);
  });

  it("keeps the exact command for retry when the reply is lost, then resends it unchanged", async () => {
    const journal = new MutationJournal(sessionStorage);
    const cancelRun = vi
      .fn()
      .mockRejectedValueOnce(new CockpitRequestError("The workshop could not be reached."))
      .mockResolvedValue({
        status: 202,
        value: v3Run({ cancellation: notCancellableBlock("already-cancelling") })
      });
    render(App, { props: { cockpitApi: api(v3Run(), { cancelRun }), mutationJournal: journal } });

    await openStagedDecision();
    await fireEvent.click(screen.getByRole("button", { name: cancel.confirm }));

    expect(await screen.findByText(cancel.uncertain)).toBeTruthy();
    const firstKey = cancelRun.mock.calls[0]?.[0]?.idempotency_key;
    await fireEvent.click(await screen.findByRole("button", { name: cancel.retry }));

    await waitFor(() => expect(cancelRun).toHaveBeenCalledTimes(2));
    expect(cancelRun.mock.calls[1]?.[0]?.idempotency_key).toBe(firstKey);
    expect(await screen.findByText(cancel.accepted)).toBeTruthy();
  });

  it("proves(a-run-that-cannot-be-cancelled-shows-why): shows the server's reason instead of a cancel button when the run cannot be cancelled", async () => {
    const run = v3Run({ cancellation: notCancellableBlock("between-nodes") });
    render(App, {
      props: { cockpitApi: api(run), mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });
    expect(
      screen.getByText(cancelReasonSentence("between-nodes", run.current_node_id))
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: cancel.open })).toBeNull();
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

function finishedNodeDetail(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "v3/two-agents",
    public_run_reference: publicReference,
    node_id: "implement",
    state: "succeeded",
    job_base64: btoa("Write three German sentences about code review."),
    job_hash: "e".repeat(64),
    answer: { value_base64: btoa("Ein gutes Code-Review."), value_hash: "f".repeat(64) },
    provenance: null,
    refusal: null,
    ...overrides
  };
}

function workingNodeDetail() {
  return {
    run_id: "v3/two-agents",
    public_run_reference: publicReference,
    node_id: "review",
    state: "working",
    job_base64: btoa("Check what the node before you did."),
    job_hash: "e".repeat(64),
    answer: null,
    provenance: null,
    refusal: null
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

    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Failed");
    expect(screen.getByRole("button", { name: "implement — Failed" }).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: /Working/ })).toBeNull();

    // A node that stopped opens on the reason it stopped, not on the first tab.
    await fireEvent.click(screen.getByRole("button", { name: "implement — Failed" }));
    expect((await screen.findByRole("tab", { name: runPageCopy.tabResult })).getAttribute("aria-selected")).toBe("true");
    await screen.findByText("Nothing written.");

    await fireEvent.click(screen.getByRole("tab", { name: runPageCopy.tabEvidence }));
    await screen.findByText("No receipt.");
    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Usage").closest("p")?.textContent).toMatch(/^Usage not recorded/);
    expect(within(who).getByText("Resolved model").closest("p")?.textContent).toMatch(
      /^Resolved model not recorded/
    );
    // The facts line under the node's title is the one place duration shows now
    // -- every tab, not only Evidence (the "Done" chip it replaces, 23.08.).
    const panel = screen.getByRole("complementary");
    await within(panel).findByText(/duration 12 s/);
    expect(within(panel).queryByText(/yet/)).toBeNull();
  });

  it("proves(a-failed-node-shows-the-stored-reason-on-the-run-page): shows the stored reason beside the state that says the run failed", async () => {
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
    expect(alert.textContent).toContain("implement");
    // The graph stays quiet: it carries state, never a paragraph of prose.
    const graph = screen.getByRole("region", { name: "Workflow" });
    expect(
      within(graph).getByRole("button", { name: "implement — Failed" }).textContent
    ).not.toContain(reason);
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

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): asks the server for that node and shows what it wrote, what it was asked and who ran it", async () => {
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
    // A finished node opens on what it produced -- whole, not a preview.
    await screen.findByText(wrote);
    await fireEvent.click(screen.getByRole("tab", { name: runPageCopy.tabPrompt }));
    await screen.findByText(asked);

    await fireEvent.click(screen.getByRole("tab", { name: runPageCopy.tabEvidence }));
    const who = await screen.findByRole("region", { name: "Who" });
    expect(who.textContent).toMatch(/builder · anthropic/);
    expect(within(who).getByText("sonnet").isConnected).toBe(true);
    // Every fingerprint shows its value beside its name, shortened, never as a
    // label a click has to solve (operator ruling 23.08.).
    for (const label of [
      runPageCopy.promptHash,
      runPageCopy.outputHash,
      runPageCopy.receiptHash
    ]) {
      expect(screen.getByRole("group", { name: label }).isConnected).toBe(true);
    }
    expect(screen.getByText(shortFingerprint("e".repeat(64))).isConnected).toBe(true);
  });

  it("groups each evidence fact with its own explanation, never a neighbour's, and says \"Seals\" once for the whole list, not once per fact", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const thisRun = await screen.findByRole("region", { name: runPageCopy.evidenceRun });
    // The list's one sentence about what a fingerprint seals stands once,
    // above the list, instead of a "Seals" wallpapered across every fact
    // beneath it (#579's befund 7.5).
    expect(within(thisRun).getByText(runPageCopy.evidenceRunIntro).isConnected).toBe(true);
    expect(within(thisRun).queryAllByText(/^Seals /)).toEqual([]);

    const groups = within(thisRun).getAllByRole("group");
    // The reading order a person meets these facts in, and the sentence each
    // one carries -- read together, inside the same accessible group, so a
    // sentence can never be mistaken for belonging to the field beside it.
    const fieldsInOrder: ReadonlyArray<{ label: string; seals: string }> = [
      { label: runPageCopy.receiptHash, seals: runPageCopy.sealsReceipt },
      { label: runPageCopy.promptHash, seals: runPageCopy.sealsPrompt },
      { label: runPageCopy.outputHash, seals: runPageCopy.sealsOutput },
      { label: runHeaderCopy.runIdLabel, seals: runHeaderCopy.sealsRunId },
      { label: runPageCopy.workflowRevision, seals: runPageCopy.sealsWorkflow },
      { label: runPageCopy.runConfiguration, seals: runPageCopy.sealsConfiguration }
    ];
    expect(groups.map((group) => group.getAttribute("aria-label"))).toEqual(
      fieldsInOrder.map((field) => field.label)
    );
    fieldsInOrder.forEach((field, index) => {
      const group = groups[index];
      if (group === undefined) throw new Error(`no evidence group rendered for ${field.label}`);
      const sentence = `${field.seals.charAt(0).toUpperCase()}${field.seals.slice(1)}.`;
      expect(within(group).getByText(sentence)).toBeTruthy();
      // No neighbour's label leaked into this group's own accessible content.
      const otherLabels = fieldsInOrder.filter((_, other) => other !== index).map((f) => f.label);
      for (const otherLabel of otherLabels) {
        expect(within(group).queryByText(otherLabel)).toBeNull();
      }
    });
  });

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): says usage is not recorded instead of leaving the question open", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Usage").closest("p")?.textContent).toMatch(/^Usage not recorded/);
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

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    // The facts line under the node's title carries duration now, replacing
    // the "Done" chip -- one grammar for every tab, not an Evidence-only fact.
    await screen.findByText(/started .* · ended .* · duration 5 min/);
  });

  it("a done wait node says it was answered instead of claiming nothing was written", async () => {
    const answeredGate = nodeDetail({
      node_id: "gate",
      state: "succeeded",
      job_base64: btoa("Merge this, or name the blocking defect."),
      job_hash: "e".repeat(64),
      answer: null,
      provenance: null
    });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => answeredGate as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabResult);

    await screen.findByText(runPageCopy.waitAnswerNotReadable);
    expect(screen.queryByText(runPageCopy.outputEmptyEnded)).toBeNull();
  });

  it("a node with a written answer shows the value, not the wait explanation", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabResult);

    await screen.findByText(wrote);
    expect(screen.queryByText(runPageCopy.waitAnswerNotReadable)).toBeNull();
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
    await openNodeTab(/review/, runPageCopy.tabResult);

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
    await openNodeTab(/review/, runPageCopy.tabResult);

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

  it("proves(a-run-page-speaks-prompt-and-output): carries the node's whole history in named tabs", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    const tabs = await screen.findByRole("tablist", { name: runPageCopy.tabsLabel });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent?.trim())).toEqual([
      runPageCopy.tabResult,
      runPageCopy.tabInput,
      runPageCopy.tabPrompt,
      runPageCopy.tabLog,
      runPageCopy.tabEvidence
    ]);
    // A finished node opens on what it produced.
    expect(
      within(tabs).getByRole("tab", { name: runPageCopy.tabResult }).getAttribute("aria-selected")
    ).toBe("true");
    expect(screen.queryByText("Asked")).toBeNull();
    expect(screen.queryByText("Answered")).toBeNull();
  });

  it("opens a waiting node on the question it asks, not on the tab that happens to be first", async () => {
    render(App, {
      props: {
        cockpitApi: api(
          v3Run({
            state: "WAITING_INPUT",
            current_node_id: "review",
            node_rail: [
              { node_id: "implement", state: "succeeded", attempt: null },
              { node_id: "review", state: "needs_you", attempt: null }
            ]
          }),
          {
            getNodeDetail: vi.fn(async () =>
              nodeDetail({ node_id: "review", state: "needs_you", answer: null }) as never
            )
          }
        ),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await fireEvent.click(await screen.findByRole("button", { name: "review — Needs you" }));

    expect(
      screen.getByRole("tab", { name: runPageCopy.tabPrompt }).getAttribute("aria-selected")
    ).toBe("true");
  });

  it("names the earlier nodes a node reads under Input, and says so honestly when it reads none", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), {
          getNodeDetail: vi.fn(async (_reference: string, nodeId: string) =>
            nodeDetail({ node_id: nodeId }) as never
          )
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await openNodeTab("review — Working", runPageCopy.tabInput);
    const reads = await screen.findByRole("tabpanel");
    expect(within(reads).getByText(runPageCopy.inputReads).isConnected).toBe(true);
    expect(within(reads).getByText("implement").isConnected).toBe(true);

    await fireEvent.click(screen.getByRole("button", { name: "review — Working" }));
    await openNodeTab("implement — Done", runPageCopy.tabInput);
    expect(
      within(screen.getByRole("tabpanel")).getByText(runPageCopy.inputNone).isConnected
    ).toBe(true);
  });

  it("proves(a-run-page-labels-the-declared-model): labels the receipt model as the declared configuration model and says a resolved model is not recorded", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("Declared model").isConnected).toBe(true);
    expect(within(who).getByText("sonnet").isConnected).toBe(true);
    const resolved = within(who).getByText("Resolved model").closest("p");
    expect(resolved?.textContent).toMatch(/^Resolved model not recorded/);
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
    await openNodeTab("review — Working", runPageCopy.tabEvidence);

    const who = await screen.findByRole("region", { name: "Who" });
    expect(within(who).getByText("No receipt yet.").isConnected).toBe(true);
    expect(within(who).queryByText("Declared model")).toBeNull();
    expect(within(who).queryByText("sonnet")).toBeNull();
    expect(within(who).getByText("Resolved model").closest("p")?.textContent).toMatch(
      /^Resolved model not recorded yet/
    );
  });
});

describe("the run page speaking the target words", () => {
  function nodeDetail() {
    return finishedNodeDetail();
  }

  it("proves(a-run-page-hash-is-a-named-proof-anchor): a fingerprint shows its value beside its name and copies in full", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { level: 1, name: "Two agents in a line" });

    // Nothing of the sort stands on the main surface.
    expect(screen.queryByText(configurationHash)).toBeNull();
    expect(screen.queryByRole("group", { name: runPageCopy.runConfiguration })).toBeNull();

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const anchor = await screen.findByRole("group", { name: runPageCopy.runConfiguration });
    expect(within(anchor).getByText(shortFingerprint(configurationHash)).isConnected).toBe(true);
    await fireEvent.click(
      within(anchor).getByRole("button", { name: `Copy ${runPageCopy.runConfiguration}` })
    );
    expect(writeText).toHaveBeenCalledWith(configurationHash);
  });

  it("proves(a-run-page-does-not-repeat-node-outputs-as-a-timeline): never pastes a finished node's output onto the run surface", async () => {
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

    const graph = await screen.findByRole("region", { name: "Workflow" });
    await waitFor(() =>
      expect(within(graph).getByRole("button", { name: "implement — Done" }).isConnected).toBe(true)
    );
    expect(document.body.textContent).not.toContain("schreiben");
    expect(screen.queryByText("As it happened")).toBeNull();
    expect(screen.queryByRole("list", { name: "What finished" })).toBeNull();
  });

  it("proves(a-run-page-leads-with-the-workflow-name): the name is the title and the run id waits in the node's evidence", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(
      (await screen.findByRole("heading", { level: 1, name: "Two agents in a line" })).isConnected
    ).toBe(true);
    expect(screen.queryByRole("heading", { level: 1, name: "Run v3/two-agents" })).toBeNull();
    // The run id names a thing; a chip that names it without showing it is a
    // riddle, so it does not stand on the main surface at all (operator, 23.08.).
    expect(screen.queryByText("v3/two-agents")).toBeNull();

    await openNodeTab(/implement/, runPageCopy.tabEvidence);

    const identity = await screen.findByRole("group", { name: "Run id" });
    expect(within(identity).getByText("v3/two-agents").isConnected).toBe(true);
  });
});
