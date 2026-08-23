import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV1 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { projectPageCopy } from "../../src/lib/projectPageCopy";
import { standingMarks, standingWords } from "../../src/lib/runState";
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

    // The finished run lives only on the second page: it is counted only if
    // the reader followed the cursor to the end.
    const work = await screen.findByRole("region", { name: projectPageCopy.workTitle });
    await within(work).findAllByRole("listitem");
    const counts = within(work)
      .getAllByRole("listitem")
      .map((entry) => entry.textContent?.replace(/\s+/g, " ").trim());

    expect(counts).toContain(`${standingMarks.running} 50 ${standingWords.running}`);
    expect(counts).toContain(`${standingMarks.done} 1 ${standingWords.done}`);
    expect(listRuns.mock.calls.map(([after]) => after)).toEqual([undefined, PAGE_CURSORS[0]]);
  });

  it("stops saying that reading further is unbuilt, because it is built", async () => {
    open("/atelier/project", pagedListRuns([firstPage, secondPage]));
    await within(
      await screen.findByRole("region", { name: projectPageCopy.workTitle })
    ).findAllByRole("listitem");

    expect(screen.queryByText(/not built yet/i)).toBeNull();
    expect(screen.queryByText(/Not every run of this project is on this page/i)).toBeNull();
  });

  it("names an incomplete initial read without confirming its partial rows", async () => {
    const listRuns = pagedListRuns([firstPage, secondPage], 1);
    window.history.replaceState(null, "", "/atelier/project");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          listRuns,
          listProjects: vi.fn(async () => ({ items: [{ public_project_reference: "project1.dGVzdA" }] }))
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const notice = await screen.findByRole("alert");

    expect(notice.textContent).toContain(projectPageCopy.runsIncomplete);
    expect(screen.queryByText(standingWords.running)).toBeNull();
    expect(screen.queryByText("run-0")).toBeNull();
    expect(screen.queryByText(standingWords.done)).toBeNull();
  });

  it("ends visibly instead of spinning when the durable list repeats a cursor", async () => {
    const listRuns = repeatingCursorListRuns(secondPage);
    window.history.replaceState(null, "", "/atelier/project");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          listRuns,
          listProjects: vi.fn(async () => ({ items: [{ public_project_reference: "project1.dGVzdA" }] }))
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const notice = await screen.findByRole("alert");

    expect(notice.textContent).toContain(projectPageCopy.runsIncomplete);
    expect(listRuns.mock.calls.length).toBeLessThanOrEqual(3);
    expect(screen.queryByText(standingWords.done)).toBeNull();
    expect(screen.queryByText("the-finished-one")).toBeNull();
  });
});
