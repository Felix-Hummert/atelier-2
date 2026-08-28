import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { AnyRun, CockpitApi, NodeDetail, RunV1, RunV3, WorkflowRevisionDetail } from "../../src/api/client";
import {
  reportConnectionLost,
  reportConnectionRestored,
  restartNoticeCopy
} from "../../src/lib/connectionState";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { wrapDisplayCopy } from "../../src/lib/displayCopy";
import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { historyWhenLabel } from "../../src/lib/historyRows";
import { standingWords } from "../../src/lib/runState";
import { cockpitApiStub } from "../support/cockpitApi";
import { notCancellableBlock } from "../support/runV3";
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
    orders: [],
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "final",
    node_rail: [{ node_id: "final", state: "succeeded", attempt: null }],
    cancellation: notCancellableBlock("already-ended"),
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
      wait_answer_schemas: [],
      node_previews: [
        { id: "final", kind: "agent", role: "builder", instruction_start: "Do the one thing.", depends_on: [] }
      ],
      loops: [],
      name,
      description: null
    }
  };
}

function nodeDetail(changes: Partial<NodeDetail> = {}): NodeDetail {
  return {
    run_id: "v3/run",
    public_run_reference: publicReference,
    node_id: "final",
    state: "succeeded",
    job_base64: btoa("job bytes must never become the result"),
    job_hash: "e".repeat(64),
    answer: null,
    provenance: null,
    refusal: null,
    refusal_output: null,
    ...changes
  };
}

function completedNodeDetail(raw = '{"answer":"PR merged"}'): NodeDetail {
  return nodeDetail({
    answer: { value_base64: btoa(raw), value_hash: "f".repeat(64) }
  });
}

function failedNodeDetail(raw = '{"answer":"could not merge"}'): NodeDetail {
  return nodeDetail({
    state: "failed",
    refusal: "schema refused",
    refusal_output: { value_base64: btoa(raw), value_hash: "a".repeat(64) }
  });
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
  reportConnectionRestored();
});

describe("History shows only what has finished", () => {
  it("says nothing finished yet, and teaches where a finished run comes from", async () => {
    openHistory({});

    expect((await screen.findByText(historyPageCopy.emptyTitle)).isConnected).toBe(true);
    const page = screen.getByRole("region", { name: "History" });
    expect(within(page).queryByRole("link", { name: /run/i })).toBeNull();
    // An empty surface is never a dead end: it names the next step and leads
    // there (operator ruling 23.08.).
    const next = within(page).getByRole("link", { name: historyPageCopy.emptyNext });
    expect(next.getAttribute("href")).toBe("/atelier/catalog");
  });

  it("says it is still looking instead of showing an empty table before the read confirms", async () => {
    openHistory({}, { listRuns: vi.fn(() => new Promise<never>(() => undefined)) });

    expect((await screen.findByText(historyPageCopy.looking)).isConnected).toBe(true);
    expect(screen.queryByText(historyPageCopy.emptyTitle)).toBeNull();
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
    expect(screen.queryByRole("button", { name: historyPageCopy.retry })).toBeNull();
  });

  it("offers Retry only after a genuine read failure, never permanently", async () => {
    const listRuns = vi.fn().mockRejectedValue(new Error("private transport detail"));
    openHistory({}, { listRuns });

    expect((await screen.findByText("History unavailable")).isConnected).toBe(true);
    expect(screen.queryByText(/private transport detail/)).toBeNull();
    const retry = screen.getByRole("button", { name: historyPageCopy.retry });

    listRuns.mockResolvedValue({ items: [], next_after: null });
    await fireEvent.click(retry);

    expect((await screen.findByText(historyPageCopy.emptyTitle)).isConnected).toBe(true);
    expect(screen.queryByText("History unavailable")).toBeNull();
    expect(screen.queryByRole("button", { name: historyPageCopy.retry })).toBeNull();
  });

  it("names no local failure while the whole workshop reads unreachable, and reads itself again once the connection returns (#700)", async () => {
    const listRuns = vi.fn().mockRejectedValue(new Error("Failed to fetch"));
    openHistory({}, { listRuns });
    await screen.findByRole("heading", { name: "History" });

    reportConnectionLost();
    await waitFor(() => {
      expect(document.querySelector(".notice-banner")?.textContent).toContain(restartNoticeCopy);
    });
    // The shell's one line above already names the outage; this page adds
    // no second, page-local echo of the same fact.
    expect(screen.queryByText("History unavailable")).toBeNull();
    expect(screen.queryByRole("button", { name: historyPageCopy.retry })).toBeNull();

    listRuns.mockResolvedValue({ items: [], next_after: null });
    reportConnectionRestored();

    expect((await screen.findByText(historyPageCopy.emptyTitle)).isConnected).toBe(true);
    expect(screen.queryByText("History unavailable")).toBeNull();
  });

  it("names the resolved workflow, when it ran, and the node result sentence", async () => {
    const run = v3Run();
    const getNodeDetail = vi.fn(async () => completedNodeDetail());
    openHistory({ completed: [run] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision("Two agents in a line")),
      getNodeDetail
    });

    const row = await screen.findByRole("link", { name: /Two agents in a line/ });
    await waitFor(() => {
      expect(getNodeDetail).toHaveBeenCalledWith(publicReference, "final");
      expect(row.textContent).toContain("PR merged");
    });
    const expectedWhen = historyWhenLabel(run.ended_at ?? "", new Date(NOW_MS));
    expect(row.querySelector("time")?.getAttribute("datetime")).toBe(run.ended_at);
    expect(row.querySelector("time")?.textContent).toContain(historyPageCopy.today);
    expect(row.querySelector("time")?.textContent).toContain(expectedWhen.clock);
    expect(row.textContent).not.toContain("just now");
    expect(row.textContent).not.toContain(standingWords.done);
    expect(row.textContent).not.toContain("job bytes must never become the result");
    expect(row.textContent).toContain("38 min");
  });

  it("leaves a completed Result empty until extras settle, never Done", async () => {
    const getNodeDetail = vi.fn(() => new Promise<NodeDetail>(() => undefined));
    openHistory({ completed: [v3Run()] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision("Two agents in a line")),
      getNodeDetail
    });

    const row = await screen.findByRole("link", { name: /Two agents in a line/ });
    const result = row.querySelector(".row-result");
    expect(result?.textContent).not.toContain(standingWords.done);
    expect(result?.textContent).not.toContain(historyPageCopy.notRecorded);
    expect(getNodeDetail).toHaveBeenCalled();
  });

  it("names a completed Result not recorded once extras settle with no readable answer, never Done", async () => {
    const getNodeDetail = vi.fn(async () => nodeDetail());
    openHistory({ completed: [v3Run()] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision("Two agents in a line")),
      getNodeDetail
    });

    const row = await screen.findByRole("link", { name: /Two agents in a line/ });
    await waitFor(() => {
      expect(row.querySelector(".row-result")?.textContent).toContain(
        wrapDisplayCopy(historyPageCopy.notRecorded)
      );
    });
    const result = row.querySelector(".row-result");
    expect(result?.textContent).not.toContain(standingWords.done);
    expect(row.textContent).not.toContain("job bytes must never become the result");
  });

  it("names the run's purpose from its own orders, with the workflow named beneath it", async () => {
    const run = v3Run({
      orders: [{ name: "diff", bytes: 12, schema_revision_hash: "d".repeat(64) }]
    });
    openHistory({ completed: [run] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision("Two agents in a line"))
    });

    const row = await screen.findByRole("link", { name: /diff/ });
    expect(within(row).getByText("diff").isConnected).toBe(true);
    expect(within(row).getByText("Two agents in a line").isConnected).toBe(true);
  });

  it("names only the workflow, once, for a run started with no orders -- nothing repeats it", async () => {
    openHistory({ completed: [v3Run({ orders: [] })] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision("Two agents in a line"))
    });

    const row = await screen.findByRole("link", { name: /Two agents in a line/ });
    expect(within(row).getAllByText("Two agents in a line")).toHaveLength(1);
  });

  it("shows the honest placeholder in the Work item column, deriving nothing", async () => {
    openHistory({ completed: [v3Run()] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision("Two agents in a line"))
    });

    const row = await screen.findByRole("link", { name: /Two agents in a line/ });
    const label = within(row).getByText("Work item:", { exact: false });
    expect(label.closest(".row-work-item")?.textContent).toContain("—");
  });

  it("names a failed run from extras, and shows no duration when no V3 stamp exists", async () => {
    const getNodeDetail = vi.fn(async () => failedNodeDetail());
    openHistory({ failed: [v1Failed({ run_id: "broke" })] }, { getNodeDetail });

    const row = await screen.findByRole("link", { name: /broke/ });
    await waitFor(() => {
      expect(getNodeDetail).toHaveBeenCalled();
      expect(row.textContent).toContain("could not merge");
    });
    expect(row.textContent).toContain(standingWords.failed);
    expect(row.textContent).not.toContain(`${standingWords.failed} ·`);
    const durationLabel = within(row).getByText("Duration:", { exact: false });
    expect(durationLabel.closest(".row-duration")?.textContent).toContain("Not recorded");
  });

  it("names a failed run's node when extras settle with no sentence", async () => {
    const getNodeDetail = vi.fn(async () => nodeDetail({ state: "failed" }));
    openHistory({ failed: [v1Failed({ run_id: "broke" })] }, { getNodeDetail });

    const row = await screen.findByRole("link", { name: /broke/ });
    await waitFor(() => {
      expect(row.textContent).toContain(`${standingWords.failed} · final`);
    });
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
    const getNodeDetail = vi.fn();
    openHistory({ completed: [old] }, {
      getWorkflowRevision: vi.fn(async () => v3Revision()),
      getNodeDetail
    });

    await screen.findByText(historyPageCopy.emptyTitle);
    expect(screen.queryByText("ancient")).toBeNull();
    expect(getNodeDetail).not.toHaveBeenCalled();
  });
});

describe("the rail leads to History rather than the old project level", () => {
  it("opens History from the Workbench's rail and reads it as an ordinary page reload would", async () => {
    window.history.replaceState(null, "", "/atelier");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub(),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("heading", { name: "Workbench" });

    const rail = screen.getByRole("navigation", { name: "Workshop" });
    await fireEvent.click(within(rail).getByRole("link", { name: "History" }));

    expect((await screen.findByRole("heading", { name: "History" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/history");
  });
});
