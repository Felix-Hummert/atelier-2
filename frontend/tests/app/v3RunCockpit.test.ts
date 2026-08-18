import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { CockpitRequestError, type CockpitApi, type RunV3 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { publicReference, revisionHash as digest } from "../support/workflowV1";

const configurationHash = "c".repeat(64);
const terminalHash = "d".repeat(64);

function v3Revision() {
  return {
    revision_hash: digest,
    document_base64: "YQ==",
    graph: {
      format_version: 3 as const,
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
      (await screen.findByRole("heading", { level: 1, name: "Run v3/two-agents" })).isConnected
    ).toBe(true);
    const graph = await screen.findByRole("region", { name: "Workflow" });
    expect(within(graph).getByRole("button", { name: "implement — Done" }).isConnected).toBe(true);
    expect(within(graph).getByRole("button", { name: "review — Working" }).isConnected).toBe(true);
    expect(screen.getByText(/not yet/i).isConnected).toBe(true);
    expect(screen.getByText(configurationHash).isConnected).toBe(true);
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
    await screen.findByRole("heading", { level: 1, name: "Run v3/two-agents" });
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
      name: "Events as they arrive"
    });
    await waitFor(() => expect(arriving.textContent).toContain("the draft"));
    expect(arriving.textContent).toContain("implement");
  });

  it("shows the terminal hash once the run has ended", async () => {
    const cockpitApi = api(
      v3Run({ state: "COMPLETED", terminal_hash: terminalHash, current_node_id: "review" })
    );

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect((await screen.findByText(terminalHash)).isConnected).toBe(true);
    expect(screen.getByLabelText("Where this run stands").textContent).toContain("Done");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("asks for the published revision so it can draw the edges, and opens the stream it can now read", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Run v3/two-agents" });
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

    await screen.findByRole("heading", { level: 1, name: "Run v3/two-agents" });
    expect(screen.getByText("Looking…").isConnected).toBe(true);
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
    expect(screen.getByRole("heading", { level: 1, name: "Run v3/two-agents" }).isConnected).toBe(
      true
    );
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

  it("proves(a-v3-line-stops-for-a-person-and-their-answer-carries-it-on): draws the node that owes a person a move as the one needing them", async () => {
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision())
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
      openRunEvents: feed.open
    });

    render(App, { props: { cockpitApi, mutationJournal: journal } });
    await screen.findByRole("button", { name: "approve — Needs you" });
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify(await waitAnsweredEvent(1)));

    expect((await screen.findByText(terminalHash)).isConnected).toBe(true);
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
      answer
    });

    render(App, { props: { cockpitApi, mutationJournal: journal } });

    expect(await screen.findByRole("heading", { name: "Answer needed" })).toBeTruthy();
    expect(screen.getByText("Wait approve")).toBeTruthy();
    await fireEvent.input(screen.getByLabelText("Answer"), {
      target: { value: '"approved, with the second paragraph rewritten"' }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Answer" }));

    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
    const mutation = answer.mock.calls[0]?.[0];
    const body = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(body).toEqual({
      revision_hash: digest,
      node_id: "approve",
      answer_base64: btoa('"approved, with the second paragraph rewritten"')
    });
    expect(await screen.findByText(terminalHash)).toBeTruthy();
  });

  it("proves(a-waiting-v3-run-is-answerable-on-its-run-page): names a refused answer on the card", async () => {
    const cockpitApi = api(waitingRun(), {
      getWorkflowRevision: vi.fn(async () => waitRevision()),
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
});


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
    await screen.findByText(/builder · anthropic · sonnet/);
  });

  it("proves(a-click-into-a-node-shows-what-it-was-asked-and-wrote): says usage and duration are not recorded instead of leaving the question open", async () => {
    render(App, {
      props: {
        cockpitApi: api(v3Run(), { getNodeDetail: vi.fn(async () => nodeDetail() as never) }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText("implement");

    await fireEvent.click(screen.getByRole("button", { name: /implement/ }));

    await screen.findByText(/not recorded yet/);
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
    await screen.findByText("review");

    await fireEvent.click(screen.getByRole("button", { name: /review/ }));

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
    await screen.findByText("review");

    await fireEvent.click(screen.getByRole("button", { name: /review/ }));

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
});
