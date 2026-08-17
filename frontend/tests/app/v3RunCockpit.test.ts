import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV3 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { publicReference, revisionHash as digest } from "../support/workflowV1";

const configurationHash = "c".repeat(64);
const terminalHash = "d".repeat(64);

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
    const rail = screen.getAllByRole("listitem").map((item) => item.textContent ?? "");
    expect(rail.some((entry) => entry.includes("implement") && /done/i.test(entry))).toBe(true);
    expect(rail.some((entry) => entry.includes("review") && /working/i.test(entry))).toBe(true);
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

  it("asks for no workflow revision, and opens the stream it can now read", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Run v3/two-agents" });
    // The revision half is unchanged and still true: it would be fetched only to
    // walk the graph's nodes, and a version 3 graph carries none. The stream
    // half is what #249 changed -- there is a format-3 event now, so declining
    // to open would be the page refusing to show what it can read.
    expect(cockpitApi.getWorkflowRevision).not.toHaveBeenCalled();
    expect(feed.open).toHaveBeenCalledTimes(1);
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
