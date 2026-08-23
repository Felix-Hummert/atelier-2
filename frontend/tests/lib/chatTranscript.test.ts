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

  it("names the vision the missing conductor belongs to, so the gap has an owner", () => {
    const [, answer] = sendChatMessage([], "start two runs");

    expect(answer?.source).toBe(chatPageCopy.conductorAbsentSource);
    expect(chatPageCopy.conductorAbsent).toContain(chatPageCopy.conductorAbsentSource);
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
