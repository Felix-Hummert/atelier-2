import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
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

  it("tells the operator it is not following the run live, and why", async () => {
    const cockpitApi = api(v3Run());

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    // The sentence says the page tells the operator. A page that merely declined
    // to open a stream would keep every other test green while saying nothing,
    // so the telling itself is asserted: the named affordance, and the reason
    // behind it.
    const why = await screen.findByRole("button", {
      name: /does not follow the run live/i
    });
    expect(screen.getByText("Snapshot").isConnected).toBe(true);
    await fireEvent.click(why);
    expect(screen.getByText(/does not follow the run live/i).isConnected).toBe(true);
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

  it("asks for no workflow revision and opens no event stream it cannot read", async () => {
    const feed = new FakeRunEventFeed();
    const cockpitApi = api(v3Run(), { openRunEvents: feed.open });

    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("heading", { level: 1, name: "Run v3/two-agents" });
    // The revision would be fetched only to walk its nodes, and a version 3
    // graph carries none; the event resource is pinned to version 2, so opening
    // a stream would claim a connection this run cannot have.
    expect(cockpitApi.getWorkflowRevision).not.toHaveBeenCalled();
    expect(feed.open).not.toHaveBeenCalled();
  });
});
