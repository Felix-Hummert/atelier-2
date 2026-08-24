import type * as SvelteTestingLibrary from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatPageCopy } from "../../src/lib/chatPageCopy";

/**
 * Chat's conversation is now owned by the `chatTranscript` module, not the
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

async function bootApp(): Promise<{
  testingLibrary: typeof SvelteTestingLibrary;
  openChat: () => void;
}> {
  vi.resetModules();
  const library = await import("@testing-library/svelte");
  const { default: App } = await import("../../src/App.svelte");
  const { MutationJournal } = await import("../../src/lib/mutationJournal");
  const { cockpitApiStub } = await import("../support/cockpitApi");

  return {
    testingLibrary: library,
    openChat: () =>
      library.render(App, {
        props: {
          cockpitApi: cockpitApiStub(),
          mutationJournal: new MutationJournal(sessionStorage)
        }
      })
  };
}

beforeEach(async () => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/chat");

  ({ testingLibrary, openChat } = await bootApp());
});

afterEach(() => testingLibrary.cleanup());

async function say(words: string): Promise<void> {
  const { fireEvent, screen } = testingLibrary;
  await fireEvent.input(screen.getByLabelText(chatPageCopy.composerLabel), {
    target: { value: words }
  });
  await fireEvent.click(screen.getByRole("button", { name: chatPageCopy.send }));
}

describe("the chat door", () => {
  it("teaches where work starts today instead of leaving an empty room, with no button duplicating the rail's own door", async () => {
    openChat();
    const { screen } = testingLibrary;

    expect((await screen.findByRole("heading", { name: "Chat" })).isConnected).toBe(true);
    expect(screen.getByText(chatPageCopy.emptyDescription).isConnected).toBe(true);
    // The rail already carries a door to Workflows; the empty state names it
    // in a sentence rather than repeating it as a second button (#579).
    expect(screen.queryByRole("link", { name: "Open Workflows" })).toBeNull();
  });

  it("keeps what was said and answers that the conductor is not connected, naming no board or issue number", async () => {
    openChat();
    const { screen, within } = testingLibrary;
    await screen.findByRole("heading", { name: "Chat" });

    await say("Finish the preview door");

    const transcript = screen.getByRole("list", { name: chatPageCopy.transcriptLabel });
    expect(within(transcript).getByText(/Finish the preview door/).isConnected).toBe(true);
    // No invented answer, no pretence that anything started, and no internal
    // vision or issue number leaked into the operator's own conversation
    // (Adressaten-Regel, operator ruling 23.08.).
    const answer = within(transcript).getByText(new RegExp("No conductor is connected"));
    expect(answer.textContent).not.toMatch(/#\d/);
  });

  it("empties the composer after sending, so the same words cannot be sent twice by accident", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Chat" });

    await say("start two runs");

    expect(screen.getByLabelText(chatPageCopy.composerLabel)).toHaveProperty("value", "");
  });

  it("takes no turn at all for a blank message", async () => {
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Chat" });

    await say("   ");

    expect(screen.queryByRole("list", { name: chatPageCopy.transcriptLabel })).toBeNull();
    expect(screen.getByText(chatPageCopy.emptyTitle).isConnected).toBe(true);
  });

  it("keeps the conversation across a rail change and back, since that is not leaving the page", async () => {
    openChat();
    const { screen, within } = testingLibrary;
    await screen.findByRole("heading", { name: "Chat" });
    await say("Finish the preview door");

    // Rail navigation tears down and rebuilds the Chat page component the
    // same way `{#if route.page === "chat"}` does in App.svelte, while the
    // module that now owns the conversation stays loaded across that swap.
    testingLibrary.cleanup();
    openChat();
    await screen.findByRole("heading", { name: "Chat" });

    const transcript = screen.getByRole("list", { name: chatPageCopy.transcriptLabel });
    expect(within(transcript).getByText(/Finish the preview door/).isConnected).toBe(true);
    expect(
      within(transcript).getByText(new RegExp("No conductor is connected")).isConnected
    ).toBe(true);
  });

  it("starts a fresh, empty conversation after a reload", async () => {
    openChat();
    await testingLibrary.screen.findByRole("heading", { name: "Chat" });
    await say("Finish the preview door");
    testingLibrary.cleanup();

    // A reload re-executes the whole module graph from scratch: a second
    // reset plus a second fresh boot is that reload, and the conductor's
    // module-owned conversation comes back empty.
    ({ testingLibrary, openChat } = await bootApp());
    openChat();
    const { screen } = testingLibrary;
    await screen.findByRole("heading", { name: "Chat" });

    expect(screen.queryByRole("list", { name: chatPageCopy.transcriptLabel })).toBeNull();
    expect(screen.getByText(chatPageCopy.emptyTitle).isConnected).toBe(true);
  });
});
