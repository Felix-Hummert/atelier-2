import { describe, expect, it } from "vitest";

import { chatPageCopy } from "../../src/lib/chatPageCopy";
import { sendChatMessage } from "../../src/lib/chatTranscript";

describe("one turn of the conversation", () => {
  it("keeps what was said and answers that no conductor is connected", () => {
    const transcript = sendChatMessage([], "Finish the preview door");

    expect(transcript.map((message) => [message.speaker, message.text])).toEqual([
      ["you", "Finish the preview door"],
      ["house", chatPageCopy.conductorAbsent]
    ]);
  });

  it("names no board or issue number in the reply, only what is true for the operator", () => {
    expect(chatPageCopy.conductorAbsent).not.toMatch(/#\d/);
  });

  it("keeps every line distinguishable across turns", () => {
    const first = sendChatMessage([], "one");
    const second = sendChatMessage(first, "two");

    expect(new Set(second.map((message) => message.id)).size).toBe(second.length);
    expect(second.map((message) => message.text)).toEqual([
      "one",
      chatPageCopy.conductorAbsent,
      "two",
      chatPageCopy.conductorAbsent
    ]);
  });

  it("drops surrounding whitespace and takes no turn at all for a blank message", () => {
    expect(sendChatMessage([], "  spaced  ")[0]?.text).toBe("spaced");

    const untouched = sendChatMessage([], "   \n ");
    expect(untouched).toEqual([]);
  });
});
