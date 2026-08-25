import type * as SvelteTestingLibrary from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";

/**
 * The Workbench's conversation is owned by the `chatTranscript` module, not the
 * page component (issue #556), so it survives the page being torn down and
 * rebuilt by in-app rail navigation. That means it also survives from one
 * test to the next unless each test gets a fresh module instance -- exactly
 * what a real reload gives the operator. `vi.resetModules()` plus a fresh
 * dynamic import of testing-library alongside the app keeps every piece
 * bound to the same reloaded Svelte runtime; mixing a freshly reset
 * component with a stale `render` from a different runtime instance fails.
 */
let testingLibrary: typeof SvelteTestingLibrary;
let openChat: () => void;
let reportConnectionLost: () => void;
let reportConnectionRestored: () => void;
let restartNoticeCopy: string;

async function bootApp(): Promise<{
  testingLibrary: typeof SvelteTestingLibrary;
  openChat: () => void;
  reportConnectionLost: () => void;
  reportConnectionRestored: () => void;
  restartNoticeCopy: string;
}> {
  vi.resetModules();
  const library = await import("@testing-library/svelte");
  const { default: App } = await import("../../src/App.svelte");
  const { MutationJournal } = await import("../../src/lib/mutationJournal");
  const { cockpitApiStub } = await import("../support/cockpitApi");
  // Loaded from the same reset module graph App.svelte binds to, so reporting
  // here reaches the exact store the composer reads (#700).
  const connection = await import("../../src/lib/connectionState");

  return {
    testingLibrary: library,
    openChat: () =>
      library.render(App, {
        props: {
          cockpitApi: cockpitApiStub(),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      }),
    reportConnectionLost: connection.reportConnectionLost,
    reportConnectionRestored: connection.reportConnectionRestored,
    restartNoticeCopy: connection.restartNoticeCopy
  };
}

beforeEach(async () => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/chat");

  ({ testingLibrary, openChat, reportConnectionLost, reportConnectionRestored, restartNoticeCopy } =
    await bootApp());
});

afterEach(() => testingLibrary.cleanup());

async function say(words: string): Promise<void> {
  const { fireEvent, screen } = testingLibrary;
  await fireEvent.input(screen.getByLabelText(workbenchPageCopy.composerLabel), {
    target: { value: words }
  });
  await fireEvent.click(screen.getByRole("button", { name: workbenchPageCopy.send }));
}

describe("the workbench door", () => {
  it("teaches where work starts today instead of leaving an empty room, with no button duplicating the rail's own door", async () => {
    openChat();
    const { screen } = testingLibrary;

    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(screen.getByText(workbenchPageCopy.emptyDescription).isConnected).toBe(true);
    // The rail already carries a door to Workflows; the empty state names it
    // in a sentence rather than repeating it as a second button (#579).
    expect(screen.queryByRole("link", { name: "Open Workflows" })).toBeNull();
  });

  it("keeps what was said and answers that nothing was started, naming no board or issue number", async () => {
    openChat();
    const { screen, within } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    await say("Finish the preview door");

    const transcript = screen.getByRole("list", { name: workbenchPageCopy.transcriptLabel });
    expect(within(transcript).getByText(/Finish the preview door/).isConnected).toBe(true);
    // No invented answer, no pretence that anything started, and no internal
    // vision or issue number leaked into the operator's own conversation
    // (Adressaten-Regel, operator ruling 23.08.).
    const answer = within(transcript).getByText(workbenchPageCopy.conductorAbsent);
    expect(answer.textContent).not.toMatch(/#\d/);
  });

  it("empties the composer after sending, so the same words cannot be sent twice by accident", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    await say("start two runs");

    expect(screen.getByLabelText(workbenchPageCopy.composerLabel)).toHaveProperty("value", "");
  });

  it("takes no turn at all for a blank message", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    await say("   ");

    expect(screen.queryByRole("list", { name: workbenchPageCopy.transcriptLabel })).toBeNull();
    expect(screen.getByText(workbenchPageCopy.emptyTitle).isConnected).toBe(true);
  });

  it("links a conductor episode with the chat as its named origin, so its trail leads back here (#654)", async () => {
    const { takeConductorTurn, markConductorRun } = await import("../../src/lib/chatTranscript");
    const { conductorChatCopy } = await import("../../src/lib/conductorChatCopy");
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    const pendingId = takeConductorTurn("Starte die Kanarienprobe", conductorChatCopy.reading);
    expect(pendingId).not.toBeNull();
    markConductorRun(pendingId ?? "", "run1.cnVu");

    const episode = await screen.findByRole("link", { name: conductorChatCopy.openEpisode });
    expect(episode.getAttribute("href")).toBe("/atelier/runs/run1.cnVu?from=chat");
  });

  it("keeps the conversation across a rail change and back, since that is not leaving the page", async () => {
    openChat();
    const { screen, within } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    await say("Finish the preview door");

    // Rail navigation tears down and rebuilds the Workbench page component the
    // same way `{#if route.page === "chat"}` does in App.svelte, while the
    // module that now owns the conversation stays loaded across that swap.
    testingLibrary.cleanup();
    openChat();
    await screen.findByRole("heading", { name: "Workbench" });

    const transcript = screen.getByRole("list", { name: workbenchPageCopy.transcriptLabel });
    expect(within(transcript).getByText(/Finish the preview door/).isConnected).toBe(true);
    expect(
      within(transcript).getByText(workbenchPageCopy.conductorAbsent).isConnected
    ).toBe(true);
  });

  it("starts a fresh, empty conversation after a reload", async () => {
    openChat();
    await testingLibrary.screen.findByRole("heading", { name: "Workbench" });
    await say("Finish the preview door");
    testingLibrary.cleanup();

    // A reload re-executes the whole module graph from scratch: a second
    // reset plus a second fresh boot is that reload, and the conductor's
    // module-owned conversation comes back empty.
    ({ testingLibrary, openChat } = await bootApp());
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });

    expect(screen.queryByRole("list", { name: workbenchPageCopy.transcriptLabel })).toBeNull();
    expect(screen.getByText(workbenchPageCopy.emptyTitle).isConnected).toBe(true);
  });

  it("disables Send and shows the restart line while the connection is lost, not the no-conductor refusal (#700)", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    await testingLibrary.fireEvent.input(screen.getByLabelText(workbenchPageCopy.composerLabel), {
      target: { value: "Finish the preview door" }
    });

    reportConnectionLost();
    await testingLibrary.waitFor(() => {
      expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
        "disabled",
        true
      );
    });

    // The global top-of-shell notice and the composer's own hint both name
    // it -- two honest readings of one store, never a page-local error.
    expect(screen.getAllByText(restartNoticeCopy).length).toBeGreaterThanOrEqual(2);
    expect(document.querySelector(".composer-hint")?.textContent).toBe(restartNoticeCopy);
    expect(screen.queryByText(workbenchPageCopy.composerHint)).toBeNull();
    // Nothing was sent: the word stays exactly where it was typed.
    expect(screen.getByLabelText(workbenchPageCopy.composerLabel)).toHaveProperty(
      "value",
      "Finish the preview door"
    );
    expect(screen.queryByRole("list", { name: workbenchPageCopy.transcriptLabel })).toBeNull();
  });

  it("re-enables Send and restores the ordinary hint once the connection returns, with no reload", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Workbench" });
    reportConnectionLost();
    await testingLibrary.waitFor(() => {
      expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
        "disabled",
        true
      );
    });

    reportConnectionRestored();

    await testingLibrary.waitFor(() => {
      expect(screen.getByRole("button", { name: workbenchPageCopy.send })).toHaveProperty(
        "disabled",
        false
      );
    });
    expect(screen.queryByText(restartNoticeCopy)).toBeNull();
    // Whichever ordinary hint the composer settles on (which conductor state
    // that is is not this test's question), it is back to something other
    // than the restart line.
    const hint = document.querySelector(".composer-hint");
    expect(hint?.textContent).not.toBe(restartNoticeCopy);
  });
});
