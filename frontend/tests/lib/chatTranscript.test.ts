import { describe, expect, it } from "vitest";

import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { sendChatMessage } from "../../src/lib/chatTranscript";

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
