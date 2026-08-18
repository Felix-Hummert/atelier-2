import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV1 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, PAGE_CURSORS } from "../support/cockpitApi";
import {
  completedRun,
  startedRun,
  waitingInputRun,
  waitingReconciliationRun
} from "../support/workflowV1";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

function listRunsByState(runs: RunV1[]) {
  return vi.fn(async (_after?: string, state?: string) => ({
    items: state === undefined ? runs : runs.filter((run) => run.state === state),
    next_after: null
  }));
}

function openStudio(runs: RunV1[] = [], overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", "/atelier");
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: listRunsByState(runs),
        ...overrides
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

describe("the studio is the level the workshop opens on", () => {
  it("proves(the-workshop-opens-in-the-studio): opens the bare atelier path in the Studio instead of a list of runs", async () => {
    openStudio();

    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Runs" })).toBeNull();
    expect(window.location.pathname).toBe("/atelier");
  });

  it("asks the durable list by the states the card and inbox can name", async () => {
    const listRuns = listRunsByState([startedRun()]);
    openStudio([startedRun()], { listRuns });
    await screen.findByRole("article", { name: "This workshop" });

    expect(listRuns.mock.calls.map(([, state]) => state).sort()).toEqual([
      "COMPLETED",
      "FAILED",
      "STARTED",
      "WAITING_INPUT",
      "WAITING_RECONCILIATION"
    ]);
  });

  it("carries one project card for this installation, with counts it can read", async () => {
    openStudio([
      startedRun({ public_run_reference: "run1.YQ" }),
      startedRun({ public_run_reference: "run1.Yg" }),
      waitingInputRun({ public_run_reference: "run1.Yw" }),
      completedRun({ public_run_reference: "run1.ZA" })
    ]);

    const card = await screen.findByRole("article", { name: "This workshop" });

    expect(within(card).getByText("2 running").isConnected).toBe(true);
    expect(within(card).getByText("1 waiting for you").isConnected).toBe(true);
    expect(within(card).getByText("1 landed").isConnected).toBe(true);
    expect(within(card).getAllByRole("link")).toHaveLength(1);
  });

  it("leads from the one project card down into the project level", async () => {
    openStudio([startedRun()]);
    const card = await screen.findByRole("article", { name: "This workshop" });

    await fireEvent.click(within(card).getByRole("link"));

    expect(window.location.pathname).toBe("/atelier/project");
    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Studio" })).toBeNull();
  });
});

describe("the inbox names what waits for a human", () => {
  it("proves(the-inbox-names-every-run-that-waits-for-a-human): names every run in a durable waiting state and no run that waits for nobody, across every page the list holds", async () => {
    // "Across everything" is only true while the reading spans the durable
    // pages: a run that waits on the second page is exactly the one an inbox
    // stopping at the first would lose.
    window.history.replaceState(null, "", "/atelier");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          listRuns: vi.fn(async (after?: string, state?: string) => {
            if (state === "WAITING_INPUT") {
              return after === undefined
                ? {
                    items: [waitingInputRun({ public_run_reference: "run1.Yg" })],
                    next_after: PAGE_CURSORS[0] ?? null
                  }
                : {
                    items: [waitingInputRun({ public_run_reference: "run1.YQ" })],
                    next_after: null
                  };
            }
            if (state === "WAITING_RECONCILIATION") {
              return {
                items: [waitingReconciliationRun({ public_run_reference: "run1.Yw" })],
                next_after: null
              };
            }
            return { items: [], next_after: null };
          })
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });

    const waiting = within(inbox).getAllByRole("link");
    expect(waiting).toHaveLength(3);
    expect(within(inbox).getAllByText("Answer")).toHaveLength(2);
    expect(within(inbox).getByText("Reconcile").isConnected).toBe(true);
  });

  it("stays silent when nothing waits for a human", async () => {
    openStudio([startedRun(), completedRun({ public_run_reference: "run1.Yg" })]);

    await screen.findByRole("article", { name: "This workshop" });

    expect(screen.queryByRole("region", { name: "Waiting for you" })).toBeNull();
  });

  it("opens the waiting run with one click", async () => {
    openStudio([waitingInputRun()], { getRun: vi.fn(async () => waitingInputRun()) });

    const inbox = await screen.findByRole("region", { name: "Waiting for you" });
    await fireEvent.click(within(inbox).getByRole("link", { name: /Answer/ }));

    await waitFor(() => expect(window.location.pathname).toBe("/atelier/runs/run1.cnVu"));
  });
});

describe("an empty studio teaches the one next action", () => {
  it("proves(an-empty-area-names-the-one-next-action): names starting a run as the one action possible today, and offers it once", async () => {
    openStudio([]);

    const empty = await screen.findByRole("heading", { name: "Nothing is running" });

    expect(empty.isConnected).toBe(true);
    expect(screen.getAllByRole("link", { name: "Start a run" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("link", { name: "Start a run" }));

    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
  });

  it("tells the truth while it is still looking, and when the read fails", async () => {
    const listRuns = vi.fn(() => new Promise<never>(() => undefined));
    openStudio([], { listRuns } as Partial<CockpitApi>);

    expect((await screen.findByText("Looking…")).isConnected).toBe(true);

    cleanup();
    openStudio([], { listRuns: vi.fn().mockRejectedValue(new Error("offline")) });

    expect((await screen.findByText(/offline/)).isConnected).toBe(true);
  });
});

describe("the chat is a named door, not a dead field", () => {
  it("names the conductor as missing instead of offering an input that answers nobody", async () => {
    openStudio([startedRun()]);

    const chat = await screen.findByRole("region", { name: "Chat" });

    expect(within(chat).getByText(/not built yet/).isConnected).toBe(true);
    expect(within(chat).queryByRole("textbox")).toBeNull();
    expect(within(chat).queryByRole("button")).toBeNull();
  });
});
