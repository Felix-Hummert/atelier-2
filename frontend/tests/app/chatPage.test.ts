import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../../src/App.svelte";
import { chatPageCopy } from "../../src/lib/chatPageCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/chat");
});

afterEach(() => cleanup());

function openChat() {
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub(),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

async function say(words: string): Promise<void> {
  await fireEvent.input(screen.getByLabelText(chatPageCopy.composerLabel), {
    target: { value: words }
  });
  await fireEvent.click(screen.getByRole("button", { name: chatPageCopy.send }));
}

describe("the chat door", () => {
  it("teaches where work starts today instead of leaving an empty room", async () => {
    openChat();

    expect((await screen.findByRole("heading", { name: "Chat" })).isConnected).toBe(true);
    expect(screen.getByText(chatPageCopy.emptyDescription).isConnected).toBe(true);
    expect(
      screen.getByRole("link", { name: chatPageCopy.emptyNext }).getAttribute("href")
    ).toBe("/atelier/workflows");
  });

  it("keeps what was said and answers that the conductor is not connected", async () => {
    openChat();
    await screen.findByRole("heading", { name: "Chat" });

    await say("Finish the preview door");

    const transcript = screen.getByRole("list", { name: chatPageCopy.transcriptLabel });
    expect(within(transcript).getByText(/Finish the preview door/).isConnected).toBe(true);
    // No invented answer, and no pretence that anything started: the missing
    // door is named with the vision that owns it.
    const answer = within(transcript).getByText(new RegExp("No conductor is connected"));
    expect(answer.textContent).toContain(chatPageCopy.conductorAbsentSource);
  });

  it("empties the composer after sending, so the same words cannot be sent twice by accident", async () => {
    openChat();
    await screen.findByRole("heading", { name: "Chat" });

    await say("start two runs");

    expect(screen.getByLabelText(chatPageCopy.composerLabel)).toHaveProperty("value", "");
  });

  it("takes no turn at all for a blank message", async () => {
    openChat();
    await screen.findByRole("heading", { name: "Chat" });

    await say("   ");

    expect(screen.queryByRole("list", { name: chatPageCopy.transcriptLabel })).toBeNull();
    expect(screen.getByText(chatPageCopy.emptyTitle).isConnected).toBe(true);
  });
});
