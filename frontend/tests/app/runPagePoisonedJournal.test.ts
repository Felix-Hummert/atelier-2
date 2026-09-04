import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../../src/App.svelte";
import PinnedDecision from "../../src/components/PinnedDecision.svelte";
import { journalPoisonedCopy } from "../../src/lib/journalPoisonedCopy";
import { cancelMutation, MutationJournal } from "../../src/lib/mutationJournal";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { MUTATION_JOURNAL_STORAGE_KEY } from "../../src/lib/storageKeys";
import { cockpitApiStub } from "../support/cockpitApi";
import {
  publicReference,
  startedRun,
  waitingInputRun,
  workflowName,
  workflowRevision
} from "../support/runV3";

/**
 * The run page's three journal read sites -- `V3RunView.loadPendingWait`,
 * `RunCancelCard`'s two reactive reads, and `PinnedDecision.loadForNode` --
 * react to a poisoned mutation journal the same way the Workbench already
 * does (#914, second half of #1131): one honest sentence, one door
 * (`journalPoisonedCopy`, `PoisonedJournalDiscardSheet.svelte`), and a
 * healed room once it is pressed, with no unhandled rejection along the way.
 */

/** jsdom has no modal dialog; the same seam `RunCancelCard`'s own staged
 * decision and the Workbench's poisoned-journal test already stub. */
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

/** Two entries sharing one mutation identity -- one of `entries()`'s own
 * rejection reasons (`mutationJournal.ts`), built from its own exported
 * `cancelMutation` rather than a hand-rolled envelope. */
function duplicateIdentityJournal(): string {
  const entry = {
    ...cancelMutation(publicReference, "d".repeat(64), "cancel-dup"),
    delivery: "prepared" as const
  };
  return JSON.stringify([entry, entry]);
}

/** Every distinct way `entries()` (`mutationJournal.ts`) refuses to read the
 * stored journal -- corrupt JSON, a value that is not a list, an entry with
 * an unknown delivery state, and a duplicate mutation identity -- so the run
 * page proves it reacts the same way regardless of which one fired. */
const POISONED_JOURNAL_FIXTURES: readonly { name: string; stored: () => string }[] = [
  { name: "invalid JSON", stored: () => "{" },
  { name: "a value that is not a list", stored: () => JSON.stringify({}) },
  { name: "an entry with an unknown delivery state", stored: () => JSON.stringify([{ delivery: "sent" }]) },
  { name: "a duplicate mutation identity", stored: duplicateIdentityJournal }
];

let unhandledRejections: unknown[] = [];

function onUnhandledRejection(reason: unknown): void {
  unhandledRejections.push(reason);
}

beforeEach(() => {
  stubDialogMethods();
  sessionStorage.clear();
  window.history.replaceState(null, "", `/atelier/runs/${publicReference}`);
  unhandledRejections = [];
  process.on("unhandledRejection", onUnhandledRejection);
});

afterEach(() => {
  process.off("unhandledRejection", onUnhandledRejection);
  cleanup();
});

/** Long enough for a promise Node has not yet reported as unhandled to be
 * reported, so an assertion right after this reads the true count. */
async function flushUnhandledRejectionQueue(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 10));
}

describe("a poisoned mutation journal on the run page (#914, second half of #1131)", () => {
  it.each(POISONED_JOURNAL_FIXTURES)(
    "proves(run-page-cancel-read-reacts-to-every-poisoned-reason): shows the one sentence and door instead of the cancel card, for $name",
    async ({ stored }) => {
      sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, stored());
      render(App, {
        props: {
          cockpitApi: cockpitApiStub({ getRun: async () => startedRun() }),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      });

      expect(await screen.findByText(journalPoisonedCopy.sentence)).toBeTruthy();
      expect(screen.getByRole("button", { name: journalPoisonedCopy.door })).toBeTruthy();
      // RunCancelCard's own reactive reads hit the identical poisoned
      // journal; it never mounts while the page stays poisoned.
      expect(screen.queryByRole("button", { name: runPageCopy.cancel.open })).toBeNull();
      expect(screen.queryByRole("heading", { level: 1 })).toBeNull();

      await flushUnhandledRejectionQueue();
      expect(unhandledRejections).toEqual([]);
    }
  );

  it("proves(run-page-wait-read-reacts-to-a-poisoned-journal): a waiting run shows the same sentence and door instead of the wait card", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({ getRun: async () => waitingInputRun() }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(await screen.findByText(journalPoisonedCopy.sentence)).toBeTruthy();
    // V3RunView's own onMount read (`loadPendingWait`) hits the identical
    // poisoned journal; it never mounts either, so neither its title nor
    // its wait card appears.
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([]);
  });

  it("proves(run-page-poisoned-journal-heals-without-a-reload): forgetting the journal heals the whole page in the same render tree", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          getRun: async () => startedRun(),
          getWorkflowRevision: async () => workflowRevision()
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByText(journalPoisonedCopy.sentence);

    await fireEvent.click(screen.getByRole("button", { name: journalPoisonedCopy.door }));
    const dialog = await screen.findByRole("dialog", { name: journalPoisonedCopy.confirmLabel });
    await fireEvent.click(within(dialog).getByRole("button", { name: journalPoisonedCopy.confirm }));

    await waitFor(() => expect(screen.queryByText(journalPoisonedCopy.sentence)).toBeNull());
    expect(sessionStorage.getItem(MUTATION_JOURNAL_STORAGE_KEY)).toBeNull();
    // The run page, gated on the same journal it just healed, renders the
    // run and its cancel control fresh -- no navigation, no App remount.
    expect(await screen.findByRole("heading", { level: 1, name: workflowName })).toBeTruthy();
    expect(await screen.findByRole("button", { name: runPageCopy.cancel.open })).toBeTruthy();
    expect(await screen.findByText(/^Forgotten at .+ — 1 byte gone\.$/)).toBeTruthy();

    const pageRoot = document.querySelector('section[aria-labelledby="v3-run-title"]');
    await waitFor(() => expect(document.activeElement).toBe(pageRoot));

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([]);
  });

  it("proves(pinned-decision-read-reacts-to-a-poisoned-journal): shows the same honest sentence instead of an unhandled rejection", async () => {
    sessionStorage.setItem(MUTATION_JOURNAL_STORAGE_KEY, "{");
    render(PinnedDecision, {
      props: {
        run: waitingInputRun(),
        workflowName,
        cockpitApi: cockpitApiStub(),
        mutationJournal: new MutationJournal(sessionStorage),
        onRunRead: () => {},
        navigate: () => {},
        onExpand: () => {}
      }
    });

    expect(await screen.findByText(journalPoisonedCopy.sentence)).toBeTruthy();

    await flushUnhandledRejectionQueue();
    expect(unhandledRejections).toEqual([]);
  });
});
