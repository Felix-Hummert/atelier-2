import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV3 } from "../../src/api/client";
import {
  reportConnectionLost,
  reportConnectionRestored,
  restartNoticeCopy
} from "../../src/lib/connectionState";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { wrapDisplayCopy } from "../../src/lib/displayCopy";
import { shortPublicRunReference } from "../../src/lib/fingerprint";
import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { historyOutcome } from "../../src/lib/historyOutcome";
import { historyWhenLabel } from "../../src/lib/historyRows";
import { standingWords } from "../../src/lib/runState";
import { cockpitApiStub } from "../support/cockpitApi";
import { notCancellableBlock } from "../support/runV3";
import { completedRun, publicReference, revisionHash } from "../support/runV3";

function failedRun(changes: Partial<RunV3> = {}): RunV3 {
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

/**
 * A finished V3 run, carrying the row's own workflow, work item and terminal
 * result (#1045) -- the same fields `RunResourceV3` serves, never a second
 * per-row read.
 */
function v3Run(changes: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/run",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    workflow_name: "Two agents in a line",
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    work_item_reference: null,
    answer: null,
    refusal_output: null,
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "final",
    node_rail: [{ node_id: "final", state: "succeeded", attempt: null }],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: revisionHash,
    latest_event_cursor: null,
    started_at: minutesAgo(38),
    ended_at: minutesAgo(0),
    ...changes,
    current_node_execution_id: changes.current_node_execution_id ?? revisionHash
  };
}

function answerOf(raw: string): NonNullable<RunV3["answer"]> {
  return { kind: "value", value_base64: btoa(raw), value_hash: "f".repeat(64) };
}

function refusalOutputOf(raw: string): NonNullable<RunV3["refusal_output"]> {
  return { value_base64: btoa(raw), value_hash: "a".repeat(64) };
}

function codeReviewAnswer(
  verdict: "approve" | "revise" | "cannot-judge",
  findings: readonly ("high" | "medium" | "low")[]
): string {
  return JSON.stringify({
    verdict,
    findings: findings.map((severity, index) => ({
      file: "secret.ts",
      line: index + 1,
      severity,
      text: "sk-live-should-never-appear"
    }))
  });
}

async function findHistoryCard(name: RegExp): Promise<HTMLElement> {
  const link = await screen.findByRole("link", { name });
  const card = link.closest(".history-row");
  if (!(card instanceof HTMLElement)) throw new Error("history row");
  return card;
}

function historyCardByRun(publicReference: string): HTMLElement {
  const link = document.querySelector(`a.history-row-open[href="/atelier/runs/${publicReference}"]`);
  const card = link?.closest(".history-row");
  if (!(card instanceof HTMLElement)) throw new Error(`history row ${publicReference}`);
  return card;
}

function visibleResultText(row: HTMLElement): string {
  const cell = row.querySelector(".row-result");
  if (!(cell instanceof HTMLElement)) return "";
  const clone = cell.cloneNode(true);
  if (!(clone instanceof HTMLElement)) return "";
  for (const hidden of clone.querySelectorAll(".visually-hidden")) {
    hidden.remove();
  }
  return (clone.textContent ?? "").replace(/\s+/g, " ").trim();
}

function openHistory(
  runsByState: { completed?: RunV3[]; failed?: RunV3[] },
  overrides: Partial<CockpitApi> = {}
) {
  window.history.replaceState(null, "", "/atelier/history");
  const listRuns = vi.fn(async (_after?: string, state?: RunV3["state"]) => ({
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
    openHistory({ completed: [v3Run()] });
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

  it("names the resolved workflow, when it ran, and a derived result that is not the raw answer, asking no node detail", async () => {
    const getNodeDetail = vi.fn();
    const run = v3Run({ answer: answerOf('{"answer":"PR merged"}') });
    openHistory({ completed: [run] }, { getNodeDetail });

    const row = await findHistoryCard(/Two agents in a line/);
    expect(row.querySelector(".row-result")?.textContent).toContain(
      historyOutcome("Two agents in a line", '{"answer":"PR merged"}')
    );
    const expectedWhen = historyWhenLabel(run.ended_at ?? "", new Date(NOW_MS));
    expect(row.querySelector("time")?.getAttribute("datetime")).toBe(run.ended_at);
    expect(row.querySelector("time")?.textContent).toContain(historyPageCopy.today);
    expect(row.querySelector("time")?.textContent).toContain(expectedWhen.clock);
    expect(row.textContent).not.toContain("just now");
    expect(row.querySelector(".row-run")?.textContent).toContain(
      shortPublicRunReference(publicReference)
    );
    expect(row.textContent).not.toContain(standingWords.done);
    expect(row.textContent).not.toContain("PR merged");
    expect(row.textContent).toContain("38 min");
    expect(getNodeDetail).not.toHaveBeenCalled();
  });

  it("names a completed code-review Result as the derived half-sentence, never Done or finding text", async () => {
    const raw = codeReviewAnswer("revise", ["high", "medium"]);
    openHistory({
      completed: [v3Run({ workflow_name: "code-review", answer: answerOf(raw) })]
    });

    const row = await findHistoryCard(/code-review/);
    const expected = historyOutcome("code-review", raw);
    expect(visibleResultText(row)).toBe(wrapDisplayCopy(expected));
    const visible = visibleResultText(row);
    expect(visible).not.toContain(standingWords.done);
    expect(visible).not.toContain("sk-live-should-never-appear");
    expect(visible).not.toContain("secret.ts");
    expect(row.textContent).not.toContain("sk-live-should-never-appear");
  });

  it("names a completed Result not recorded when the run carries no readable answer, never Done", async () => {
    openHistory({ completed: [v3Run({ answer: null })] });

    const row = await findHistoryCard(/Two agents in a line/);
    expect(row.querySelector(".row-result")?.textContent).toContain(
      wrapDisplayCopy(historyPageCopy.notRecorded)
    );
    const result = row.querySelector(".row-result");
    expect(result?.textContent).not.toContain(standingWords.done);
  });

  it("names an omitted answer too large to show, never the same Not recorded a run that wrote nothing shows (#1045)", async () => {
    openHistory({
      completed: [
        v3Run({
          answer: { kind: "omitted", reason: "too_large", maximum_bytes: 49_152 }
        })
      ]
    });

    const row = await findHistoryCard(/Two agents in a line/);
    expect(row.querySelector(".row-result")?.textContent).toContain(
      wrapDisplayCopy(historyPageCopy.answerTooLarge)
    );
    expect(row.querySelector(".row-result")?.textContent).not.toContain(
      wrapDisplayCopy(historyPageCopy.notRecorded)
    );
  });

  it("names the run's purpose as the workflow, never the order names", async () => {
    const run = v3Run({
      orders: [{ name: "diff", bytes: 12, schema_revision_hash: "d".repeat(64) }]
    });
    openHistory({ completed: [run] });

    const row = await findHistoryCard(/Two agents in a line/);
    expect(row.querySelector(".row-purpose")?.textContent).toBe("Two agents in a line");
    expect(row.querySelector(".row-name")?.textContent).not.toContain("diff");
    expect(row.querySelector(".row-workflow")).toBeNull();
  });

  it("does not put typical code-review order names in the purpose cell", async () => {
    const run = v3Run({
      workflow_name: "code-review",
      orders: [
        { name: "context", bytes: 12, schema_revision_hash: "d".repeat(64) },
        { name: "diff", bytes: 12, schema_revision_hash: "d".repeat(64) },
        { name: "review_questions", bytes: 12, schema_revision_hash: "d".repeat(64) }
      ]
    });
    openHistory({ completed: [run] });

    const row = await findHistoryCard(/code-review/);
    const purpose = row.querySelector(".row-purpose")?.textContent ?? "";
    expect(purpose).toBe("code-review");
    expect(purpose).not.toContain("context");
    expect(purpose).not.toContain("diff");
    expect(purpose).not.toContain("review_questions");
  });

  it("names only the workflow, once, for a run started with no orders -- nothing repeats it", async () => {
    openHistory({ completed: [v3Run({ orders: [] })] });

    const row = await findHistoryCard(/Two agents in a line/);
    const purpose = row.querySelector(".row-name");
    expect(purpose).not.toBeNull();
    expect(within(purpose as HTMLElement).getAllByText("Two agents in a line")).toHaveLength(1);
  });

  it("shows the honest placeholder in the Work item column when no work item hangs on the run", async () => {
    openHistory({ completed: [v3Run()] });

    const row = await findHistoryCard(/Two agents in a line/);
    const label = within(row).getByText("Work item:", { exact: false });
    expect(label.closest(".row-work-item")?.textContent).toContain("—");
  });

  it("names the work item as a link from the run's own reference, asking no node detail", async () => {
    const getNodeDetail = vi.fn();
    const run = v3Run({
      workflow_name: "code-review",
      work_item_reference: "gh:567",
      answer: answerOf(codeReviewAnswer("approve", []))
    });
    openHistory({ completed: [run] }, {
      getNodeDetail,
      getProjectSourceConnection: vi.fn(async () => ({
        public_project_reference: "project1.YQ",
        revision_number: 1,
        source_kind: "github",
        source_address: "FlexOr2/atelier-2@main",
        auth_method: "personal-access-token" as const,
        project_source_connection_revision_hash: "d".repeat(64)
      })),
      listProjects: vi.fn(async () => ({ items: [{ public_project_reference: "project1.YQ" }] }))
    });

    const row = await findHistoryCard(/code-review/);
    await waitFor(() => {
      expect(row.querySelector(".row-work-item")?.textContent).toContain("#567");
    });
    const itemLink = within(row).getByRole("link", { name: "#567" });
    expect(itemLink.getAttribute("href")).toBe("https://github.com/FlexOr2/atelier-2/issues/567");
    expect(row.querySelector(".row-result")?.textContent).toContain(
      historyPageCopy.outcome.approved
    );
    expect(getNodeDetail).not.toHaveBeenCalled();
  });

  it("names a failed run from its own refusal output, and shows no duration when no stamp exists, asking no node detail", async () => {
    const getNodeDetail = vi.fn();
    openHistory(
      {
        failed: [
          failedRun({
            run_id: "broke",
            refusal_output: refusalOutputOf('{"answer":"could not merge"}')
          })
        ]
      },
      { getNodeDetail }
    );

    const row = await findHistoryCard(/Four steps in a line/);
    expect(row.querySelector(".row-result")?.textContent).toContain(
      historyOutcome("Four steps in a line", '{"answer":"could not merge"}')
    );
    expect(row.textContent).toContain(standingWords.failed);
    expect(row.textContent).not.toContain("could not merge");
    expect(row.textContent).not.toContain(`${standingWords.failed} ·`);
    const durationLabel = within(row).getByText("Duration:", { exact: false });
    expect(durationLabel.closest(".row-duration")?.textContent).toContain("Not recorded");
    expect(getNodeDetail).not.toHaveBeenCalled();
  });

  it("names a failed run's node when it carries no refusal output", async () => {
    openHistory({ failed: [failedRun({ run_id: "broke", refusal_output: null })] });

    const row = await findHistoryCard(/Four steps in a line/);
    expect(row.textContent).toContain(`${standingWords.failed} · final`);
  });

  it("leads down into the same run page a live run would open, already frozen", async () => {
    const landed = v3Run();
    openHistory({ completed: [landed] }, { getRun: vi.fn(async () => landed) });

    await fireEvent.click(await screen.findByRole("link", { name: /Two agents in a line/ }));

    expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`);
  });

  it("never hides a timestampless row behind the period chip, and names why", async () => {
    openHistory({ completed: [completedRun({ run_id: "old-format" })] });

    const row = await findHistoryCard(/Four steps in a line/);
    expect(row.isConnected).toBe(true);
    expect(
      screen.getByText(/Runs with no recorded time always show here/).isConnected
    ).toBe(true);
  });

  it("shows no timestampless hint when every listed row carries a real stamp", async () => {
    openHistory({ completed: [v3Run()] });
    await screen.findByRole("link", { name: /Two agents in a line/ });

    expect(screen.queryByText(/Runs with no recorded time/)).toBeNull();
  });

  it("renders two same-workflow runs on the same day differently when work item and outcome differ", async () => {
    const ended = minutesAgo(5);
    const started = minutesAgo(10);
    const first = v3Run({
      public_run_reference: "run1.YQ",
      run_id: "first",
      workflow_name: "code-review",
      started_at: started,
      ended_at: ended,
      work_item_reference: "gh:567",
      answer: answerOf(codeReviewAnswer("revise", ["high", "medium", "low"]))
    });
    const second = v3Run({
      public_run_reference: "run1.Yg",
      run_id: "second",
      workflow_name: "code-review",
      started_at: started,
      ended_at: ended,
      work_item_reference: "gh:840",
      answer: answerOf(codeReviewAnswer("approve", []))
    });
    openHistory({ completed: [first, second] });

    await waitFor(() => {
      expect(historyCardByRun("run1.YQ").textContent).toContain("#567");
      expect(historyCardByRun("run1.Yg").textContent).toContain("#840");
    });
    const firstRow = historyCardByRun("run1.YQ");
    const secondRow = historyCardByRun("run1.Yg");
    expect(firstRow.textContent).not.toBe(secondRow.textContent);
    expect(firstRow.querySelector(".row-result")?.textContent).not.toBe(
      secondRow.querySelector(".row-result")?.textContent
    );
  });

  it("renders two runs that differ only in their result as different rows", async () => {
    const ended = minutesAgo(5);
    const started = minutesAgo(10);
    const first = v3Run({
      public_run_reference: "run1.YQ",
      run_id: "alpha",
      workflow_name: "code-review",
      started_at: started,
      ended_at: ended,
      work_item_reference: "gh:567",
      answer: answerOf(codeReviewAnswer("revise", ["high"]))
    });
    const second = v3Run({
      public_run_reference: "run1.Yg",
      run_id: "beta",
      workflow_name: "code-review",
      started_at: started,
      ended_at: ended,
      work_item_reference: "gh:567",
      answer: answerOf(codeReviewAnswer("approve", []))
    });
    openHistory({ completed: [first, second] });

    await waitFor(() => {
      expect(historyCardByRun("run1.YQ").querySelector(".row-result")?.textContent).toContain(
        historyPageCopy.outcome.revise
      );
      expect(historyCardByRun("run1.Yg").querySelector(".row-result")?.textContent).toContain(
        historyPageCopy.outcome.approved
      );
    });
    const firstRow = historyCardByRun("run1.YQ");
    const secondRow = historyCardByRun("run1.Yg");
    expect(firstRow.textContent).not.toBe(secondRow.textContent);
  });

  it("renders two runs identical in time, work item and outcome as different rows", async () => {
    const ended = minutesAgo(5);
    const started = minutesAgo(10);
    const shared = {
      workflow_name: "code-review",
      started_at: started,
      ended_at: ended,
      work_item_reference: "gh:567",
      answer: answerOf(codeReviewAnswer("approve", []))
    };
    const first = v3Run({ public_run_reference: "run1.YQ", run_id: "alpha", ...shared });
    const second = v3Run({ public_run_reference: "run1.Yg", run_id: "beta", ...shared });
    openHistory({ completed: [first, second] });

    await waitFor(() => {
      expect(historyCardByRun("run1.YQ").querySelector(".row-result")?.textContent).toContain(
        historyPageCopy.outcome.approved
      );
      expect(historyCardByRun("run1.Yg").querySelector(".row-result")?.textContent).toContain(
        historyPageCopy.outcome.approved
      );
    });
    const firstRow = historyCardByRun("run1.YQ");
    const secondRow = historyCardByRun("run1.Yg");
    expect(firstRow.querySelector(".row-when")?.textContent).toBe(
      secondRow.querySelector(".row-when")?.textContent
    );
    expect(firstRow.querySelector(".row-work-item")?.textContent).toBe(
      secondRow.querySelector(".row-work-item")?.textContent
    );
    expect(firstRow.querySelector(".row-result")?.textContent).toBe(
      secondRow.querySelector(".row-result")?.textContent
    );
    expect(firstRow.querySelector(".row-run")?.textContent).toContain(
      shortPublicRunReference("run1.YQ")
    );
    expect(secondRow.querySelector(".row-run")?.textContent).toContain(
      shortPublicRunReference("run1.Yg")
    );
    expect(firstRow.textContent).not.toBe(secondRow.textContent);
  });

  it("never renders raw result bytes, including a secret in an unknown payload", async () => {
    const secret = "sk-live-should-never-appear";
    const run = v3Run({
      workflow_name: "hello-atelier",
      answer: answerOf(JSON.stringify({ token: secret, note: "keep out" }))
    });
    openHistory({ completed: [run] });

    const row = await findHistoryCard(/hello-atelier/);
    expect(row.querySelector(".row-result")?.textContent).toContain("2 fields");
    expect(row.textContent).not.toContain(secret);
    expect(row.textContent).not.toContain("keep out");
  });

  it("keeps a run outside the 7 day window off the list, honestly reporting nothing recent", async () => {
    const old = v3Run({ run_id: "ancient", ended_at: "2020-01-01T00:00:00Z" });
    openHistory({ completed: [old] });

    await screen.findByText(historyPageCopy.emptyTitle);
    expect(screen.queryByText("ancient")).toBeNull();
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
