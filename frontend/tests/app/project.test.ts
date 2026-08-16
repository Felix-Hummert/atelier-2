import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV1 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import {
  completedRun,
  publicReference,
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
});

function openAt(pathname: string, overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", pathname);
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub(overrides),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

const openProject = (runs: RunV1[], overrides: Partial<CockpitApi> = {}) =>
  openAt("/atelier/project", {
    listRuns: vi.fn(async () => ({ items: runs, next_after: null })),
    ...overrides
  });

describe("the project answers what is happening here", () => {
  it("heads the level with the one project of this installation", async () => {
    openProject([startedRun()]);

    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
  });

  it("groups the runs by what each one is doing, and omits a group nothing is in", async () => {
    openProject([
      startedRun({ public_run_reference: "run1.YQ", run_id: "alpha" }),
      waitingInputRun({ public_run_reference: "run1.Yg", run_id: "beta" }),
      startedRun({ public_run_reference: "run1.Yw", run_id: "gamma" })
    ]);

    const running = await screen.findByRole("region", { name: "Running" });
    expect(within(running).getAllByRole("link")).toHaveLength(2);
    expect(within(await screen.findByRole("region", { name: "Waiting for you" })).getAllByRole("link")).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Done" })).toBeNull();
  });

  it("lets a row carry the move a human owes and the group carry the state", async () => {
    openProject([
      startedRun({ public_run_reference: "run1.YQ", run_id: "alpha" }),
      waitingInputRun({ public_run_reference: "run1.Yg", run_id: "beta" }),
      waitingReconciliationRun({ public_run_reference: "run1.Yw", run_id: "gamma" }),
      completedRun({ public_run_reference: "run1.ZA", run_id: "delta" })
    ]);

    const waiting = await screen.findByRole("region", { name: "Waiting for you" });
    expect(within(waiting).getByText("Answer").isConnected).toBe(true);
    expect(within(waiting).getByText("Reconcile").isConnected).toBe(true);

    for (const group of ["Running", "Done"]) {
      const rows = within(screen.getByRole("region", { name: group })).getAllByRole("link");
      expect(rows.map((row) => row.textContent?.trim())).toEqual(
        group === "Running" ? ["alpha"] : ["delta"]
      );
    }
  });

  it("leads down into a run of this project", async () => {
    openProject([startedRun()]);

    const running = await screen.findByRole("region", { name: "Running" });
    await fireEvent.click(within(running).getByRole("link"));

    expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`);
  });

  it("keeps confirmed runs visible when a refresh fails, and says what failed", async () => {
    const listRuns = vi.fn().mockResolvedValue({ items: [startedRun()], next_after: null });
    openProject([], { listRuns });
    await screen.findByRole("region", { name: "Running" });

    listRuns.mockRejectedValueOnce(new Error("offline"));
    await fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect((await screen.findByRole("alert")).textContent).toContain("offline");
    expect(screen.getByRole("region", { name: "Running" }).isConnected).toBe(true);
  });

  it("says it is still looking instead of showing a project with nothing in it", async () => {
    openProject([], { listRuns: vi.fn(() => new Promise<never>(() => undefined)) });

    expect((await screen.findByText("Looking…")).isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: "Running" })).toBeNull();
  });
});

describe("the queue names what does not exist yet", () => {
  it("names the absent ranking and offers the one action possible today, once", async () => {
    openProject([startedRun()]);

    const queue = await screen.findByRole("region", { name: "Queue" });

    expect(
      within(queue).getByText("This project has no priority and no assignment yet.").isConnected
    ).toBe(true);
    expect(within(queue).queryByText(/order|first|next|schedul|priorit\w+ is/i)).toBeNull();
    expect(screen.getAllByRole("link", { name: "Start a run" })).toHaveLength(1);

    await fireEvent.click(within(queue).getByRole("link", { name: "Start a run" }));

    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
  });

  it("hints at no rule, no source, and no assignment the system does not have", async () => {
    openProject([startedRun()]);
    const queue = await screen.findByRole("region", { name: "Queue" });

    expect(within(queue).queryByRole("button")).toBeNull();
    expect(screen.queryByRole("region", { name: /Rules|Sources|Settings|Library/ })).toBeNull();
  });
});

describe("every level names the way back up", () => {
  it("proves(every-level-names-the-way-back-up): walks the named way from the run up to the project and from the project up into the studio", async () => {
    const feed = new FakeRunEventFeed();
    openAt(`/atelier/runs/${publicReference}`, {
      getRun: vi.fn(async () => startedRun()),
      getWorkflowRevision: vi.fn(async () => workflowRevision()),
      openRunEvents: feed.open,
      listRuns: vi.fn(async () => ({ items: [startedRun()], next_after: null }))
    });
    await screen.findByRole("heading", { name: "Run run" });

    await fireEvent.click(screen.getByRole("link", { name: "← Project" }));

    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/project");

    await fireEvent.click(screen.getByRole("link", { name: "← Studio" }));

    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });
});
