import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
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
 * `every-level-names-the-way-back-up`) both serve the retired REQ-UI-01, and
 * retiring them formally is the requirement revision's job (#521), not this
 * file's — so these tests carry no claim.
 */
describe("every page carries one way back and does not repeat its own title", () => {
  it("leads a run back to the Board, the rail destination it belongs to", async () => {
    open(RUN_PATH);
    await screen.findByRole("heading", { name: "Unnamed workflow" });

    const back = screen.getByRole("navigation", { name: "Where you are" });
    expect(within(back).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "←Board"
    ]);
    expect(within(back).queryByText("Unnamed workflow")).toBeNull();

    await fireEvent.click(within(back).getByRole("link", { name: "Board" }));

    expect((await screen.findByRole("heading", { name: "Board" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });

  it("leads the project back to the Board without naming the project twice", async () => {
    open("/atelier/project");
    await screen.findByRole("heading", { name: THE_ONE_PROJECT });

    const back = screen.getByRole("navigation", { name: "Where you are" });
    expect(within(back).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "←Board"
    ]);
    expect(within(back).queryByText(THE_ONE_PROJECT)).toBeNull();
  });

  it("leads the start door back to Workflows, where starting a run belongs", async () => {
    open("/atelier/new");
    await screen.findByRole("heading", { name: "Choose a workflow" });

    const back = screen.getByRole("navigation", { name: "Where you are" });

    expect(within(back).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "←Workflows"
    ]);
  });
});
