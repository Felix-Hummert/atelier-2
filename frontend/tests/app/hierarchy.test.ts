import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { backLinkCopy } from "../../src/lib/backLinkCopy";
import { THE_ONE_PROJECT } from "../../src/lib/project";
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

/**
 * Where a page leads back to, and what it never repeats.
 *
 * The trail that walked studio → project → run is gone with the ordering the
 * mockup and the operator's 23.08. ruling put in its place: a page carries one
 * way back and never restates its own title in a crumb beside it. The two
 * acceptance sentences that pinned the old trail
 * (`the-deepest-level-shows-the-whole-way-it-sits-on`,
 * `every-level-names-the-way-back-up`) served the superseded REQ-UI-01 and were
 * retired with it in `acceptance/131-the-workshop-has-three-levels.toml`
 * (#552) — so these tests carry no claim, and there is no sentence left for
 * them to carry.
 */
describe("every page carries one way back and does not repeat its own title", () => {
  it("leads a run back to the Workbench, the room living work belongs to", async () => {
    open(RUN_PATH);
    await screen.findByRole("heading", { name: "Unnamed workflow" });

    const back = screen.getByRole("navigation", { name: backLinkCopy.whereYouAre });
    expect(within(back).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "←Workbench"
    ]);
    expect(within(back).queryByText("Unnamed workflow")).toBeNull();

    await fireEvent.click(within(back).getByRole("link", { name: "Workbench" }));

    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/chat");
  });

  // Settings is the context above the rooms, reached from the rail's foot in
  // every room, so it carries no way back of its own -- a second door to the
  // room you just left is exactly what ADR 0019 removes.
  it("gives Settings no trail of its own, because the rail is its one door", async () => {
    open("/atelier/settings");
    await screen.findByRole("heading", { name: THE_ONE_PROJECT });

    expect(screen.queryByRole("navigation", { name: backLinkCopy.whereYouAre })).toBeNull();
  });

});
