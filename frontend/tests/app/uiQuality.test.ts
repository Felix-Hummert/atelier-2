import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { encodePublicRunReference, type RunV1 } from "../../src/api/client";
import { wrapDisplayCopy } from "../../src/lib/displayCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { humanMove, standingWords } from "../../src/lib/runState";
import { connectionLabels } from "../../src/lib/streamStatus";
import { studioPageCopy } from "../../src/lib/studioPageCopy";
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

const OWNED_RAIL = ["[[[ Chat ]]]", "[[[ Board ]]]", "[[[ Workflows ]]]", "[[[ History ]]]"];

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

function populatedStudioRuns(): RunV1[] {
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

function openStudioPseudoLocale(listRuns: ReturnType<typeof vi.fn>) {
  window.history.replaceState(null, "", "/atelier?pseudo-locale=1");
  const feed = new FakeRunEventFeed();
  render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns,
        openAttentionEvents: feed.openAttention
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
  return feed;
}

function openEmptyStudio() {
  window.history.replaceState(null, "", "/atelier?pseudo-locale=1");
  const feed = new FakeRunEventFeed();
  render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: vi.fn(async () => ({ items: [], next_after: null })),
        openAttentionEvents: feed.openAttention
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
  return feed;
}

function openProjectPseudoLocale() {
  window.history.replaceState(null, "", "/atelier/project?pseudo-locale=1");
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: vi.fn(async () => ({ items: [], next_after: null })),
        listProjects: vi.fn(async () => ({ items: [{ public_project_reference: "project1.dGVzdA" }] }))
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

// The one project's real name (#133 seam) and the "·" punctuation between the
// Settings and Profile chips are the only rail text a pseudo-locale wrap does
// not own — everything else the rail renders must come from a copy owner.
const RAIL_TEXT_EXCEPTIONS = new Set([THE_ONE_PROJECT, "·"]);

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
    if (text !== "" && !RAIL_TEXT_EXCEPTIONS.has(text) && !isPseudoLocaleWrapped(text)) {
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
  it("proves(studio-entry-copy-is-owned-and-survives-pseudo-locale): Studio renders its header and confirmed empty copy through the display transform", async () => {
    const feed = openEmptyStudio();

    await screen.findByRole("heading", { name: "[[[ Board ]]]" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: "[[[ Nothing is running ]]]" });

    expect(screen.getByText("[[[ Atelier ]]]").isConnected).toBe(true);
    expect(screen.getByText("[[[ A workflow becomes a run, and a run is what this workshop shows. ]]]").isConnected).toBe(true);
    expect(screen.getByRole("link", { name: "[[[ Start a run ]]]" }).isConnected).toBe(true);
  });

  it("proves(studio-populated-copy-is-owned-and-survives-pseudo-locale): Studio renders inbox, card counts, chat, and connection through the display transform", async () => {
    openStudioPseudoLocale(listRunsByState(populatedStudioRuns()));

    const inbox = await screen.findByRole("region", { name: wrapDisplayCopy(standingWords.waiting) });
    expect(within(inbox).getByText(`2 ${wrapDisplayCopy(studioPageCopy.needYou)}`).isConnected).toBe(true);
    expect(within(inbox).getAllByText(wrapDisplayCopy(studioPageCopy.needsYou))).toHaveLength(2);
    const answer = humanMove("WAITING_INPUT");
    const reconcile = humanMove("WAITING_RECONCILIATION");
    expect(answer).not.toBeNull();
    expect(reconcile).not.toBeNull();
    expect(within(inbox).getByText(wrapDisplayCopy(answer ?? "")).isConnected).toBe(true);
    expect(within(inbox).getByText(wrapDisplayCopy(reconcile ?? "")).isConnected).toBe(true);

    expect(screen.getByRole("heading", { name: wrapDisplayCopy(studioPageCopy.projects) }).isConnected).toBe(true);
    const card = await screen.findByRole("article", { name: THE_ONE_PROJECT });
    expect(within(card).getByText(`2 ${wrapDisplayCopy(studioPageCopy.runningCount)}`).isConnected).toBe(true);
    expect(within(card).getByText(`2 ${wrapDisplayCopy(studioPageCopy.waitingCount)}`).isConnected).toBe(true);
    expect(within(card).getByText(`1 ${wrapDisplayCopy(studioPageCopy.failedCount)}`).isConnected).toBe(true);
    expect(within(card).getByText(`1 ${wrapDisplayCopy(studioPageCopy.landedCount)}`).isConnected).toBe(true);

    const chat = screen.getByRole("region", { name: wrapDisplayCopy(studioPageCopy.chat) });
    expect(within(chat).getByText(wrapDisplayCopy(studioPageCopy.chatUnavailable)).isConnected).toBe(true);
    expect(screen.getByText(wrapDisplayCopy(connectionLabels.connecting)).isConnected).toBe(true);
  });

  it("proves(studio-populated-copy-is-owned-and-survives-pseudo-locale): Studio renders both failed-read titles through the display transform", async () => {
    openStudioPseudoLocale(vi.fn().mockRejectedValue(new Error("wire detail")));
    expect((await screen.findByText(wrapDisplayCopy(studioPageCopy.runsUnavailable))).isConnected).toBe(true);

    cleanup();
    openStudioPseudoLocale(
      vi.fn(async (after?: string, state?: string) => {
        if (state === "STARTED" && after === undefined) {
          return { items: [startedRun()], next_after: "run1.bmV4dA" };
        }
        if (state === "STARTED") throw new Error("later page detail");
        return { items: [], next_after: null };
      })
    );
    expect((await screen.findByText(wrapDisplayCopy(studioPageCopy.runsIncomplete))).isConnected).toBe(true);
    expect(screen.queryByText(/later page detail/)).toBeNull();
  });

  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): Board rail uses the owner, not a hardcoded copy", async () => {
    open("/atelier?pseudo-locale=1");
    await screen.findByRole("heading", { name: "[[[ Board ]]]" });
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

  it("Project renders its new work-first copy through the display transform", async () => {
    openProjectPseudoLocale();

    await screen.findByRole("heading", { name: THE_ONE_PROJECT });
    expect(screen.getByText("[[[ Project ]]]").isConnected).toBe(true);
    expect(screen.getByRole("link", { name: "[[[ Start a run ]]]" }).isConnected).toBe(true);
    expect(screen.getByRole("heading", { name: "[[[ Queue ]]]" }).isConnected).toBe(true);
    expect(screen.getByText("[[[ No priority or assignment. ]]]").isConnected).toBe(true);
  });
});
