import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { publicReference, startedRun, workflowRevision } from "../support/workflowV1";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  window.history.replaceState(null, "", "/atelier");
});

const OWNED_RAIL = ["[[[ Studio ]]]", "[[[ Projekte ]]]", "[[[ Runs ]]]", "[[[ Library ]]]", "[[[ Settings ]]]"];

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

async function railShowsOwnedPseudoLocale(): Promise<void> {
  const rail = await screen.findByRole("navigation", { name: "Workshop" });
  const labels = within(rail)
    .getAllByText((_, element) => element?.classList.contains("nav-destination-label") === true)
    .map((node) => node.textContent);
  expect(labels).toEqual(OWNED_RAIL);
}

describe("core surfaces read owned display strings", () => {
  it("proves(studio-entry-copy-is-owned-and-survives-pseudo-locale): Studio renders its header and confirmed empty copy through the display transform", async () => {
    const feed = openEmptyStudio();

    await screen.findByRole("heading", { name: "[[[ Studio ]]]" });
    feed.handlers?.opened();
    await screen.findByRole("heading", { name: "[[[ Nothing is running ]]]" });

    expect(screen.getByText("[[[ Atelier ]]]").isConnected).toBe(true);
    expect(screen.getByText("[[[ A workflow becomes a run, and a run is what this workshop shows. ]]]").isConnected).toBe(true);
    expect(screen.getByRole("link", { name: "[[[ Start a run ]]]" }).isConnected).toBe(true);
  });

  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): Studio rail uses the owner, not a hardcoded copy", async () => {
    open("/atelier?pseudo-locale=1");
    await screen.findByRole("heading", { name: "[[[ Studio ]]]" });
    await railShowsOwnedPseudoLocale();
  });

  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): New Run rail uses the owner, not a hardcoded copy", async () => {
    open("/atelier/new?pseudo-locale=1");
    await screen.findByRole("heading", { name: "Choose a workflow" });
    await railShowsOwnedPseudoLocale();
  });

  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): Run page rail uses the owner, not a hardcoded copy", async () => {
    open(`/atelier/runs/${publicReference}?pseudo-locale=1`);
    await screen.findByRole("heading", { name: "Run run" });
    await railShowsOwnedPseudoLocale();
  });

  it("Project renders its new work-first copy through the display transform", async () => {
    openProjectPseudoLocale();

    await screen.findByRole("heading", { name: "This workshop" });
    expect(screen.getByText("[[[ Project ]]]").isConnected).toBe(true);
    expect(screen.getByRole("link", { name: "[[[ Start a run ]]]" }).isConnected).toBe(true);
    expect(screen.getByRole("heading", { name: "[[[ Queue ]]]" }).isConnected).toBe(true);
    expect(screen.getByText("[[[ No priority or assignment. ]]]").isConnected).toBe(true);
  });
});
