import { cleanup, render, screen, waitFor } from "@testing-library/svelte";
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
