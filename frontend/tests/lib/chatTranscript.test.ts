import { describe, expect, it } from "vitest";

import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import {
  appendConductorTurn,
  resolveConductorLine,
  sendChatMessage,
  withRunReference
} from "../../src/lib/chatTranscript";

describe("one turn of the conversation", () => {
  it("keeps what was said and answers that nothing was started", () => {
    const transcript = sendChatMessage([], "Finish the preview door");

    expect(transcript.map((message) => [message.speaker, message.text])).toEqual([
      ["you", "Finish the preview door"],
      ["house", workbenchPageCopy.conductorAbsent]
    ]);
  });

  it("names no board or issue number in the reply, only what is true for the operator", () => {
    expect(workbenchPageCopy.conductorAbsent).not.toMatch(/#\d/);
  });

  it("keeps every line distinguishable across turns", () => {
    const first = sendChatMessage([], "one");
    const second = sendChatMessage(first, "two");

    expect(new Set(second.map((message) => message.id)).size).toBe(second.length);
    expect(second.map((message) => message.text)).toEqual([
      "one",
      workbenchPageCopy.conductorAbsent,
      "two",
      workbenchPageCopy.conductorAbsent
    ]);
  });

  it("drops surrounding whitespace and takes no turn at all for a blank message", () => {
    expect(sendChatMessage([], "  spaced  ")[0]?.text).toBe("spaced");

    const untouched = sendChatMessage([], "   \n ");
    expect(untouched).toEqual([]);
  });
});

describe("one conductor turn of the conversation", () => {
  it("holds the reply's place with a pending line until its episode settles it", () => {
    const opened = appendConductorTurn([], "Start the canary", "reading…");
    expect(opened).not.toBeNull();
    if (opened === null) return;

    expect(opened.transcript.map((line) => [line.speaker, line.text, line.pending ?? false])).toEqual([
      ["you", "Start the canary", false],
      ["house", "reading…", true]
    ]);

    const settled = resolveConductorLine(opened.transcript, opened.pendingId, "Done.");
    expect(settled.map((line) => [line.text, line.pending ?? false])).toEqual([
      ["Start the canary", false],
      ["Done.", false]
    ]);
  });

  it("attaches the episode's run to exactly the pending line", () => {
    const opened = appendConductorTurn([], "hello", "reading…");
    expect(opened).not.toBeNull();
    if (opened === null) return;

    const linked = withRunReference(opened.transcript, opened.pendingId, "run1.abc");

    expect(linked.map((line) => line.runReference)).toEqual([undefined, "run1.abc"]);
  });

  it("takes no turn for a blank message", () => {
    expect(appendConductorTurn([], "  \n ", "reading…")).toBeNull();
  });
});
