import { workbenchPageCopy } from "./workbenchPageCopy";

/** Who said a line: the operator, or the house answering for itself. */
export type ChatSpeaker = "you" | "house";

export type ChatMessage = {
  /** Stable within one transcript, so a keyed list never re-creates a rendered line. */
  id: string;
  speaker: ChatSpeaker;
  text: string;
};

/**
 * One turn of the conversation while whether a conductor is connected could
 * not yet be told one way or the other -- the composer is not locked here
 * the way it is for "absent", "unbound" and "not-startable" (#1103), each of
 * which carries a real reason and refuses the message before this function
 * is ever called: what was said, and the honest standing answer.
 *
 * Pure and total, so the page holds no branching of its own. Blank input is
 * not a turn — an empty message would put an empty bubble in the transcript
 * and pull a reply out of the house for nothing.
 *
 * The house answer is the same sentence every time on purpose: nothing reads
 * these messages yet, and a varying reply would suggest something is
 * listening.
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
    { id: `you-${turn}`, speaker: "you", text },
    { id: `house-${turn}`, speaker: "house", text: workbenchPageCopy.conductorConnectionUnknown }
  ];
}

/**
 * The conversation as this module holds it, so it outlives the Workbench page
 * component being torn down and rebuilt by in-app rail navigation -- to the
 * operator that is not "closing the page" the reply promises to keep talking
 * about (Klarheitsvertrag Punkt 12, Epic #516). A reload starts a fresh
 * module instance and empties this honestly; carrying a conversation across a
 * reload is a durable-conversation door owned by #7, not built yet.
 */
let moduleTranscript: readonly ChatMessage[] = [];

/**
 * Listeners exist because a conductor reply lands asynchronously, possibly
 * after in-app navigation rebuilt the page: whoever renders the conversation
 * subscribes instead of polling module state.
 */
const listeners = new Set<(transcript: readonly ChatMessage[]) => void>();

function publish(next: readonly ChatMessage[]): void {
  moduleTranscript = next;
  for (const listener of listeners) listener(next);
}

export function currentChatTranscript(): readonly ChatMessage[] {
  return moduleTranscript;
}

export function subscribeChatTranscript(
  listener: (transcript: readonly ChatMessage[]) => void
): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Sends one no-conductor turn against the module-owned conversation. */
export function sendChatTurn(typed: string): readonly ChatMessage[] {
  publish(sendChatMessage(moduleTranscript, typed));
  return moduleTranscript;
}
