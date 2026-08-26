import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { encodePublicRunReference, type RunV1 } from "../../src/api/client";
import { wrapDisplayCopy } from "../../src/lib/displayCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { humanMove } from "../../src/lib/runState";
import { settingsPageCopy } from "../../src/lib/settingsPageCopy";
import { standingWords } from "../../src/lib/runState";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import {
  completedRun,
  publicReference,
  revisionHash,
  startedRun,
  waitingInputRun,
  waitingReconciliationRun,
  workflowRevision
} from "../support/workflowV1";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  window.history.replaceState(null, "", "/atelier");
});

const OWNED_RAIL = [
  "[[[ Workbench ]]]",
  "[[[ Catalog ]]]",
  "[[[ History ]]]",
  "[[[ Settings ]]]"
];

function open(pathname: string) {
  window.history.replaceState(null, "", pathname);
  const feed = new FakeRunEventFeed();
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: vi.fn(async () => ({ items: [startedRun()], next_after: null })),
        getRun: vi.fn(async () => startedRun()),
        getWorkflowRevision: vi.fn(async () => workflowRevision()),
        openRunEvents: feed.open
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

function listRunsByState(runs: RunV1[]) {
  return vi.fn(async (_after?: string, state?: string) => ({
    items: state === undefined ? runs : runs.filter((run) => run.state === state),
    next_after: null
  }));
}

function listedRun(runId: string, factory: (changes?: Partial<RunV1>) => RunV1, extra: Partial<RunV1> = {}): RunV1 {
  return factory({
    run_id: runId,
    public_run_reference: encodePublicRunReference(runId),
    latest_event_cursor: null,
    ...extra
  });
}

function populatedWorkbenchRuns(): RunV1[] {
  const reconciliation = waitingReconciliationRun();
  if (reconciliation.waiting.type !== "WAITING_RECONCILIATION") {
    throw new Error("waiting reconciliation fixture must wait for reconciliation");
  }
  return [
    listedRun("run-a", startedRun),
    listedRun("run-b", startedRun),
    listedRun("wait-a", waitingInputRun),
    listedRun("wait-b", waitingReconciliationRun, {
      waiting: { ...reconciliation.waiting, node_id: reconciliation.current_node.node_id }
    }),
    listedRun("fail-a", startedRun, { state: "FAILED", terminal_hash: revisionHash }),
    listedRun("done-a", completedRun)
  ];
}

function openWorkbenchPseudoLocale(listRuns: ReturnType<typeof vi.fn>) {
  window.history.replaceState(null, "", "/atelier?pseudo-locale=1");
  render(App, {
    props: {
      cockpitApi: cockpitApiStub({ listRuns }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

function openProjectPseudoLocale() {
  window.history.replaceState(null, "", "/atelier/project?pseudo-locale=1");
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: vi.fn(async () => ({ items: [], next_after: null })),
        listProjects: vi.fn(async () => ({ items: [{ public_project_reference: "project1.dGVzdA" }] })),
        getProjectSourceConnection: vi.fn(async () => ({
          public_project_reference: "project1.dGVzdA",
          revision_number: 1,
          source_kind: "github",
          source_address: "atelier/atelier-2",
          auth_method: "personal-access-token" as const,
          project_source_connection_revision_hash: "a".repeat(64)
        })),
        getProjectModelDefaults: vi.fn(async () => ({
          project_id: "atelier",
          public_project_reference: "project1.dGVzdA",
          revision_number: 1,
          project_model_defaults_revision_hash: "b".repeat(64),
          defaults: []
        }))
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

// The one project's real name (#133 seam) is the only rail text a
// pseudo-locale wrap does not own — everything else the rail renders must come
// from a copy owner.
const RAIL_TEXT_EXCEPTIONS = new Set([THE_ONE_PROJECT]);

function isPseudoLocaleWrapped(text: string): boolean {
  return text.startsWith("[[[") && text.endsWith("]]]");
}

async function railShowsOwnedPseudoLocale(): Promise<void> {
  const rail = await screen.findByRole("navigation", { name: "Workshop" });
  const labels = within(rail)
    .getAllByText((_, element) => element?.classList.contains("nav-destination-label") === true)
    .map((node) => node.textContent);
  expect(labels).toEqual(OWNED_RAIL);

  const walker = document.createTreeWalker(rail, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      node.parentElement?.closest('[aria-hidden="true"]') == null
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT
  });
  const unowned: string[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const text = node.textContent?.trim() ?? "";
    // A bare digit string (the rail's ochre count) is data, not copy: a count
    // needs no translation wrap, the same way a run id or count elsewhere on
    // the workshop never does.
    if (
      text !== "" &&
      !RAIL_TEXT_EXCEPTIONS.has(text) &&
      !isPseudoLocaleWrapped(text) &&
      !/^\d+$/.test(text)
    ) {
      unowned.push(text);
    }
  }
  expect(unowned, `unowned rail copy escapes the pseudo-locale wrap: ${unowned.join("; ")}`).toEqual([]);

  const unwrappedTitles = [...rail.querySelectorAll("[title]")]
    .map((element) => element.getAttribute("title") ?? "")
    .filter((title) => title !== "" && !isPseudoLocaleWrapped(title));
  expect(unwrappedTitles, `unwrapped rail title: ${unwrappedTitles.join("; ")}`).toEqual([]);
}

describe("core surfaces read owned display strings", () => {
  // The identifiers stay "studio-…" (acceptance/435): the room they measure is
  // the Workbench since ADR 0019 retired the Board.
  it("proves(studio-entry-copy-is-owned-and-survives-pseudo-locale): the Workbench renders its header and confirmed empty copy through the display transform", async () => {
    openWorkbenchPseudoLocale(vi.fn(async () => ({ items: [], next_after: null })));

    await screen.findByRole("heading", { name: "[[[ Workbench ]]]" });
    await screen.findByRole("heading", { name: `[[[ ${workbenchPageCopy.emptyTitle} ]]]` });

    expect(screen.getByText(wrapDisplayCopy(workbenchPageCopy.emptyDescription)).isConnected).toBe(
      true
    );
    expect(
      screen.getByRole("link", { name: wrapDisplayCopy(workbenchPageCopy.emptyStart) }).isConnected
    ).toBe(true);
  });

  it("proves(studio-populated-copy-is-owned-and-survives-pseudo-locale): the Workbench renders its row moves through the display transform, and never lists a terminal run (#667)", async () => {
    openWorkbenchPseudoLocale(listRunsByState(populatedWorkbenchRuns()));

    const answer = humanMove("WAITING_INPUT");
    const reconcile = humanMove("WAITING_RECONCILIATION");
    expect(answer).not.toBeNull();
    expect(reconcile).not.toBeNull();
    expect((await screen.findByText(`${wrapDisplayCopy(answer ?? "")} →`)).isConnected).toBe(true);
    expect(screen.getByText(`${wrapDisplayCopy(reconcile ?? "")} →`).isConnected).toBe(true);

    // The fail-a and done-a fixtures in this same set are terminal: the
    // Workbench never lists their state at all, so they leave nothing behind
    // here for History to duplicate (#667).
    expect(screen.queryByText(wrapDisplayCopy(standingWords.failed))).toBeNull();
    expect(screen.queryByText(wrapDisplayCopy(standingWords.done))).toBeNull();
  });

  it("proves(studio-populated-copy-is-owned-and-survives-pseudo-locale): the Workbench renders both failed-read titles through the display transform", async () => {
    openWorkbenchPseudoLocale(vi.fn().mockRejectedValue(new Error("wire detail")));
    expect(
      (await screen.findByText(wrapDisplayCopy(workbenchPageCopy.runsUnavailable))).isConnected
    ).toBe(true);

    cleanup();
    openWorkbenchPseudoLocale(
      vi.fn(async (after?: string, state?: string) => {
        if (state === "STARTED" && after === undefined) {
          return { items: [startedRun()], next_after: "run1.bmV4dA" };
        }
        if (state === "STARTED") throw new Error("later page detail");
        return { items: [], next_after: null };
      })
    );
    expect(
      (await screen.findByText(wrapDisplayCopy(workbenchPageCopy.runsIncomplete))).isConnected
    ).toBe(true);
    expect(screen.queryByText(/later page detail/)).toBeNull();
  });

  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): the Workbench rail uses the owner, not a hardcoded copy", async () => {
    open("/atelier?pseudo-locale=1");
    await screen.findByRole("heading", { name: "[[[ Workbench ]]]" });
    await railShowsOwnedPseudoLocale();
  });

  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): New Run rail uses the owner, not a hardcoded copy", async () => {
    open("/atelier/new?pseudo-locale=1");
    await screen.findByRole("heading", { name: "Choose a workflow" });
    await railShowsOwnedPseudoLocale();
  });

  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): Run page rail uses the owner, not a hardcoded copy", async () => {
    open(`/atelier/runs/${publicReference}?pseudo-locale=1`);
    await screen.findByRole("heading", { name: "Unnamed workflow" });
    await railShowsOwnedPseudoLocale();
  });

  it("Settings renders its own copy through the display transform", async () => {
    openProjectPseudoLocale();

    await screen.findByRole("heading", { name: THE_ONE_PROJECT });
    expect(
      (await screen.findByRole("heading", { name: wrapDisplayCopy(settingsPageCopy.sourcesTitle) })).isConnected
    ).toBe(true);
    expect(
      screen.getByRole("heading", { name: wrapDisplayCopy(settingsPageCopy.modelsTitle) })
        .isConnected
    ).toBe(true);
  });
});
