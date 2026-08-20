import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV1 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import {
  cockpitApiStub,
  PAGE_CURSORS,
  pagedListRuns,
  repeatingCursorListRuns
} from "../support/cockpitApi";
import { completedRun, startedRun, waitingInputRun } from "../support/workflowV1";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

/** A first page as full as the durable route serves one, carrying no terminal run. */
const firstPage: readonly RunV1[] = Array.from({ length: 50 }, (_, index) =>
  startedRun({ public_run_reference: `run1.p${index}`, run_id: `run-${index}` })
);

/** The second page, holding the only Done run and the only run that waits. */
const secondPage: readonly RunV1[] = [
  completedRun({ public_run_reference: "run1.q0", run_id: "the-finished-one" }),
  waitingInputRun({ public_run_reference: "run1.q1", run_id: "the-waiting-one" })
];

function open(pathname: string, listRuns: CockpitApi["listRuns"]) {
  window.history.replaceState(null, "", pathname);
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({ listRuns }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

describe("the workshop reads the durable list to its end", () => {
  it("proves(the-workshop-reads-every-page-or-says-it-could-not): shows a group that lives only on the second page, following the cursor until it ends", async () => {
    const listRuns = pagedListRuns([firstPage, secondPage]);
    open("/atelier/project", listRuns);

    const done = await screen.findByRole("region", { name: "Done" });

    expect(within(done).getByText("the-finished-one").isConnected).toBe(true);
    expect(within(await screen.findByRole("region", { name: "Running" })).getAllByRole("link")).toHaveLength(50);
    expect(listRuns.mock.calls.map(([after]) => after)).toEqual([undefined, PAGE_CURSORS[0]]);
  });

  it("stops saying that reading further is unbuilt, because it is built", async () => {
    open("/atelier/project", pagedListRuns([firstPage, secondPage]));
    await screen.findByRole("region", { name: "Done" });

    expect(screen.queryByText(/not built yet/i)).toBeNull();
    expect(screen.queryByText(/Not every run of this project is on this page/i)).toBeNull();
  });

  it("names an incomplete initial read without confirming its partial rows", async () => {
    open("/atelier/project", pagedListRuns([firstPage, secondPage], 1));

    const notice = await screen.findByRole("alert");

    expect(notice.textContent).toContain("Project runs incomplete");
    expect(screen.queryByRole("region", { name: "Running" })).toBeNull();
    expect(screen.queryByText("run-0")).toBeNull();
    expect(screen.queryByRole("region", { name: "Done" })).toBeNull();
  });

  it("ends visibly instead of spinning when the durable list repeats a cursor", async () => {
    const listRuns = repeatingCursorListRuns(secondPage);
    open("/atelier/project", listRuns);

    const notice = await screen.findByRole("alert");

    expect(notice.textContent).toContain("Project runs incomplete");
    expect(listRuns.mock.calls.length).toBeLessThanOrEqual(3);
    expect(screen.queryByRole("region", { name: "Done" })).toBeNull();
    expect(screen.queryByText("the-finished-one")).toBeNull();
  });
});
