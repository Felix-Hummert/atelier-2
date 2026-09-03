import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunPage } from "../../src/api/client";
import { journalPoisonedCopy } from "../../src/lib/journalPoisonedCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { MUTATION_JOURNAL_STORAGE_KEY } from "../../src/lib/storageKeys";
import { cockpitApiStub } from "../support/cockpitApi";
import { waitingInputRun } from "../support/runV3";

/**
 * A poisoned mutation journal shows one honest sentence and one door on the
 * Workbench, the one surface this slice covers (#914; the run page follows
 * separately). The door is never called "Discard" -- that word already
 * belongs, in `runPageCopy`, to leaving a single uncertain send unresolved --
 * and its confirmation is the catalog's own three-fact retire card, per the
 * mockup's own note that the pattern needs no new vocabulary.
 */

/** jsdom has no modal dialog; the same seam RunCancelCard's staged decision uses. */
function stubDialogMethods(): void {
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: {
      configurable: true,
      value(this: HTMLDialogElement): void {
        this.open = true;
      }
    },
    close: {
      configurable: true,
      value(this: HTMLDialogElement): void {
        this.open = false;
      }
    }
  });
}

function listRunsWaiting(waiting: readonly ReturnType<typeof waitingInputRun>[]) {
  return async (_after?: string, state?: string): Promise<RunPage> => ({
    items: state === "WAITING_INPUT" ? [...waiting] : [],
    next_after: null
  });
}

function openWorkbench(overrides: Partial<CockpitApi> = {}): void {
  render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listRuns: listRunsWaiting([waitingInputRun()]),
        ...overrides
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

beforeEach(() => {
  stubDialogMethods();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/chat");
});

afterEach(() => cleanup());

describe("a poisoned mutation journal on the Workbench", () => {
  it("shows one sentence and one door, and mounts no card that would have read the same poisoned journal", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    openWorkbench();

    expect(await screen.findByText(journalPoisonedCopy.sentence)).toBeTruthy();
    expect(screen.getByRole("button", { name: journalPoisonedCopy.door })).toBeTruthy();
    // The pinned decision for the WAITING_INPUT run would read this exact
    // journal to show itself; it never mounts while the room stays poisoned.
    expect(screen.queryByRole("heading", { name: runPageCopy.needsYou })).toBeNull();
  });

  it("shows nothing when the journal reads cleanly", async () => {
    openWorkbench();

    await screen.findByRole("heading", { name: "Workbench" });
    expect(screen.queryByText(journalPoisonedCopy.sentence)).toBeNull();
  });

  it("the door opens the retire card's own three facts, and confirming heals the room without a reload", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    openWorkbench();
    await screen.findByText(journalPoisonedCopy.sentence);

    await fireEvent.click(screen.getByRole("button", { name: journalPoisonedCopy.door }));

    const dialog = await screen.findByRole("dialog", { name: journalPoisonedCopy.confirmLabel });
    expect(
      within(dialog).getByRole("heading", { name: journalPoisonedCopy.confirmQuestion })
    ).toBeTruthy();
    expect(within(dialog).getByText(journalPoisonedCopy.disappears)).toBeTruthy();
    expect(within(dialog).getByText(journalPoisonedCopy.stays)).toBeTruthy();
    expect(within(dialog).getByText(journalPoisonedCopy.permanent)).toBeTruthy();

    await fireEvent.click(within(dialog).getByRole("button", { name: journalPoisonedCopy.confirm }));

    // Healed in the same render tree, no reload: the notice retires, the
    // journal's own storage is gone with it, and the pin that was reading it
    // takes over the room on its own.
    await waitFor(() => expect(screen.queryByText(journalPoisonedCopy.sentence)).toBeNull());
    expect(sessionStorage.getItem(MUTATION_JOURNAL_STORAGE_KEY)).toBeNull();
    expect(await screen.findByRole("heading", { name: runPageCopy.needsYou })).toBeTruthy();
  });

  it("cancelling the confirmation leaves the poisoned journal exactly as it was", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    openWorkbench();
    await screen.findByText(journalPoisonedCopy.sentence);

    await fireEvent.click(screen.getByRole("button", { name: journalPoisonedCopy.door }));
    const dialog = await screen.findByRole("dialog", { name: journalPoisonedCopy.confirmLabel });
    await fireEvent.click(within(dialog).getByRole("button", { name: journalPoisonedCopy.cancel }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText(journalPoisonedCopy.sentence)).toBeTruthy();
    expect(sessionStorage.getItem(MUTATION_JOURNAL_STORAGE_KEY)).toBe("{");
  });
});
