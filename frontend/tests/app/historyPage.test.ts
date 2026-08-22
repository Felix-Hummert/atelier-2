import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { AnyRun, CockpitApi, RunV1, RunV3, WorkflowRevisionDetail } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";
import { completedRun, publicReference, revisionHash } from "../support/workflowV1";

function v1Failed(changes: Partial<RunV1> = {}): RunV1 {
  return { ...completedRun(changes), state: "FAILED" };
}

/**
 * Real, moving timestamps rather than a fixed calendar date: the period
 * filter compares a row's real V3 stamp against the page's own wall-clock
 * `now`, so a fixture anchored to a fixed past date would drift out of the
 * 7 day window as the calendar advances and turn this suite flaky. Anchoring
 * to `Date.now()` at load keeps "ended a fixed number of minutes ago" true no
 * matter when the suite runs, while the elapsed span itself (what "38 min"
 * asserts on) stays exact.
 */
const NOW_MS = Date.now();

function minutesAgo(minutes: number): string {
  return new Date(NOW_MS - minutes * 60_000).toISOString();
}

function v3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/run",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "final",
    node_rail: [{ node_id: "final", state: "succeeded", attempt: null }],
    terminal_hash: revisionHash,
    latest_event_cursor: null,
    started_at: minutesAgo(38),
    ended_at: minutesAgo(0),
    ...changes
  };
}

function v3Revision(name = "Two agents in a line", hash = revisionHash): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: hash,
    document_base64: "",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["builder"],
      orders: [],
      node_previews: [
        { id: "final", kind: "agent", role: "builder", instruction_start: "Do the one thing.", depends_on: [] }
      ],
      loops: [],
      name,
      description: null
    }
  };
}

function openHistory(
  runsByState: { completed?: AnyRun[]; failed?: AnyRun[] },
  overrides: Partial<CockpitApi> = {}
) {
  window.history.replaceState(null, "", "/atelier/history");
  const listRuns = vi.fn(async (_after?: string, state?: AnyRun["state"]) => ({
    items: state === "FAILED" ? runsByState.failed ?? [] : runsByState.completed ?? [],
    next_after: null
  }));
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({ listRuns, ...overrides }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("History shows only what has finished", () => {
  it("says nothing finished yet rather than showing an empty table", async () => {
    openHistory({});

    expect((await screen.findByText("No finished runs yet")).isConnected).toBe(true);
    const page = screen.getByRole("region", { name: "History" });
    expect(within(page).queryByRole("link")).toBeNull();
  });

  it("says it is still looking instead of showing an empty table before the read confirms", async () => {
    openHistory({}, { listRuns: vi.fn(() => new Promise<never>(() => undefined)) });

    expect((await screen.findByText("Looking…")).isConnected).toBe(true);
    expect(screen.queryByText("No finished runs yet")).toBeNull();
  });

  it("shows the silent 7 day period chip and no Start, permanent Refresh or Queue affordance", async () => {
    openHistory({ completed: [v3Run()] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision())
    });
    await screen.findByRole("link", { name: /Two agents in a line/ });

    expect(screen.getByText("7 days").isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Start a run" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Queue" })).toBeNull();
    // One freshness model: a loaded page carries no permanent Refresh control
    // (mockup v5 §05 shows none) -- Retry appears only on a genuine read failure.
    expect(screen.queryByRole("button", { name: /Refresh/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("offers Retry only after a genuine read failure, never permanently", async () => {
    const listRuns = vi.fn().mockRejectedValue(new Error("private transport detail"));
    openHistory({}, { listRuns });

    expect((await screen.findByText("History unavailable")).isConnected).toBe(true);
    expect(screen.queryByText(/private transport detail/)).toBeNull();
    const retry = screen.getByRole("button", { name: "Retry" });

    listRuns.mockResolvedValue({ items: [], next_after: null });
    await fireEvent.click(retry);

    expect((await screen.findByText("No finished runs yet")).isConnected).toBe(true);
    expect(screen.queryByText("History unavailable")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("names the resolved workflow, a plain Completed result and the real duration", async () => {
    openHistory({ completed: [v3Run()] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision("Two agents in a line"))
    });

    const row = await screen.findByRole("link", { name: /Two agents in a line/ });
    expect(row.textContent).toContain("Completed");
    expect(row.textContent).toContain("38 min");
  });

  it("names a failed run's node and shows no duration when no V3 stamp exists", async () => {
    openHistory({ failed: [v1Failed({ run_id: "broke" })] });

    const row = await screen.findByRole("link", { name: /broke/ });
    expect(row.textContent).toContain("Failed at final");
    expect(row.textContent).toContain("Not recorded");
  });

  it("leads down into the same run page a live run would open, already frozen", async () => {
    const landed = v3Run();
    openHistory({ completed: [landed] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision()),
      getRun: vi.fn(async () => landed)
    });

    await fireEvent.click(await screen.findByRole("link", { name: /Two agents in a line/ }));

    expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`);
  });

  it("never hides a timestampless V1 row behind the period chip, and names why", async () => {
    openHistory({ completed: [completedRun({ run_id: "old-format" })] });

    const row = await screen.findByRole("link", { name: /old-format/ });
    expect(row.isConnected).toBe(true);
    expect(
      screen.getByText(/Runs with no recorded time always show here/).isConnected
    ).toBe(true);
  });

  it("shows no timestampless hint when every listed row carries a real stamp", async () => {
    openHistory({ completed: [v3Run()] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision())
    });
    await screen.findByRole("link", { name: /Two agents in a line/ });

    expect(screen.queryByText(/Runs with no recorded time/)).toBeNull();
  });

  it("keeps a run outside the 7 day window off the list, honestly reporting nothing recent", async () => {
    const old = v3Run({ run_id: "ancient", ended_at: "2020-01-01T00:00:00Z" });
    openHistory({ completed: [old] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision())
    });

    await screen.findByText("No finished runs yet");
    expect(screen.queryByText("ancient")).toBeNull();
  });
});

describe("the rail leads to History rather than the old project level", () => {
  it("opens History from the Board's rail and reads it as an ordinary page reload would", async () => {
    window.history.replaceState(null, "", "/atelier");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub(),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: "Board" });

    const rail = screen.getByRole("navigation", { name: "Workshop" });
    await fireEvent.click(within(rail).getByRole("link", { name: "History" }));

    expect((await screen.findByRole("heading", { name: "History" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/history");
  });
});
