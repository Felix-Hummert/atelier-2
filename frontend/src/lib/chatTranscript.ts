import { chatPageCopy } from "./chatPageCopy";

/** Who said a line: the operator, or the house answering for itself. */
export type ChatSpeaker = "you" | "house";

export type ChatMessage = {
  /** Stable within one transcript, so a keyed list never re-creates a rendered line. */
  id: string;
  speaker: ChatSpeaker;
  text: string;
  /** The vision or issue a house line comes from, or null where it speaks for itself. */
  source: string | null;
};

/**
 * One turn of the conversation: what was said, and what the house answers.
 *
 * Pure and total, so the page holds no branching of its own. Blank input is
 * not a turn — an empty message would put an empty bubble in the transcript
 * and pull a reply out of the house for nothing.
 *
 * The house answer is the same sentence every time on purpose: no conductor
 * reads these messages yet (Vision #7), and a varying reply would suggest
 * something is listening.
 */
export function sendChatMessage(
  transcript: readonly ChatMessage[],
  typed: string
): readonly ChatMessage[] {
  const text = typed.trim();
  if (text.length === 0) return transcript;
  const turn = transcript.length + 1;
  return [
    ...transcript,
    { id: `you-${turn}`, speaker: "you", text, source: null },
    {
      id: `house-${turn}`,
      speaker: "house",
      text: chatPageCopy.conductorAbsent,
      source: chatPageCopy.conductorAbsentSource
    }
  ];
}
