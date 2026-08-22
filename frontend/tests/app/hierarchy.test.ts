import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { publicReference, startedRun, workflowRevision } from "../support/workflowV1";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

const RUN_PATH = `/atelier/runs/${publicReference}`;

function open(pathname: string, overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", pathname);
  const feed = new FakeRunEventFeed();
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: vi.fn(async () => ({ items: [startedRun()], next_after: null })),
        getRun: vi.fn(async () => startedRun()),
        getWorkflowRevision: vi.fn(async () => workflowRevision()),
        openRunEvents: feed.open,
        ...overrides
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

describe("the run knows where it sits", () => {
  it("proves(the-deepest-level-shows-the-whole-way-it-sits-on): shows the whole trail from the run, and walks every step of it", async () => {
    open(RUN_PATH);
    await screen.findByRole("heading", { name: "Unnamed workflow" });

    const trail = screen.getByRole("navigation", { name: "Where you are" });
    expect(within(trail).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "Studio",
      "This workshop"
    ]);
    expect(within(trail).getByText("Unnamed workflow").isConnected).toBe(true);

    await fireEvent.click(within(trail).getByRole("link", { name: "This workshop" }));
    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/project");

    await fireEvent.click(
      within(screen.getByRole("navigation", { name: "Where you are" })).getByRole("link", {
        name: "Studio"
      })
    );
    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });

  it("names the level it is on without offering it as a step to walk", async () => {
    open("/atelier/project");
    await screen.findByRole("heading", { name: "This workshop" });

    const trail = screen.getByRole("navigation", { name: "Where you are" });

    expect(within(trail).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "Studio"
    ]);
    expect(within(trail).getByText("This workshop").isConnected).toBe(true);
    expect(screen.queryByRole("link", { name: "← Studio" })).toBeNull();
  });
});
