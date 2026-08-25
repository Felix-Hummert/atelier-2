import { workbenchPageCopy } from "./workbenchPageCopy";

/** Who said a line: the operator, or the house answering for itself. */
export type ChatSpeaker = "you" | "house";

export type ChatMessage = {
  /** Stable within one transcript, so a keyed list never re-creates a rendered line. */
  id: string;
  speaker: ChatSpeaker;
  text: string;
  /** The conductor episode this line reports on, so the page can link its run. */
  runReference?: string;
  /** True while the episode still runs and this line only holds its place. */
  pending?: boolean;
};

/**
 * One turn of the conversation while NO conductor is connected: what was said,
 * and the honest standing answer.
 *
 * Pure and total, so the page holds no branching of its own. Blank input is
 * not a turn — an empty message would put an empty bubble in the transcript
 * and pull a reply out of the house for nothing.
 *
 * The house answer is the same sentence every time on purpose: no conductor
 * reads these messages, and a varying reply would suggest something is
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
    { id: `house-${turn}`, speaker: "house", text: workbenchPageCopy.conductorAbsent }
  ];
}

/**
 * One turn while a conductor IS connected: the operator's line, and a pending
 * house line that holds the reply's place until the episode ends. Returns the
 * pending line's id so the episode can settle exactly this line later, or
 * null for a blank message that takes no turn.
 */
export function appendConductorTurn(
  transcript: readonly ChatMessage[],
  typed: string,
  pendingText: string
): { transcript: readonly ChatMessage[]; pendingId: string } | null {
  const text = typed.trim();
  if (text.length === 0) return null;
  const turn = transcript.length + 1;
  const pendingId = `house-${turn}`;
  return {
    transcript: [
      ...transcript,
      { id: `you-${turn}`, speaker: "you", text },
      { id: pendingId, speaker: "house", text: pendingText, pending: true }
    ],
    pendingId
  };
}

/** The pending line learns which run carries its episode, and keeps waiting. */
export function withRunReference(
  transcript: readonly ChatMessage[],
  pendingId: string,
  runReference: string
): readonly ChatMessage[] {
  return transcript.map((message) =>
    message.id === pendingId ? { ...message, runReference } : message
  );
}

/** The episode ended: the pending line becomes its final text and stops waiting. */
export function resolveConductorLine(
  transcript: readonly ChatMessage[],
  pendingId: string,
  text: string
): readonly ChatMessage[] {
  return transcript.map((message) =>
    message.id === pendingId ? { ...message, text, pending: false } : message
  );
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

/** Opens one conductor turn and returns the pending line's id, null for blank. */
export function takeConductorTurn(typed: string, pendingText: string): string | null {
  const turn = appendConductorTurn(moduleTranscript, typed, pendingText);
  if (turn === null) return null;
  publish(turn.transcript);
  return turn.pendingId;
}

export function markConductorRun(pendingId: string, runReference: string): void {
  publish(withRunReference(moduleTranscript, pendingId, runReference));
}

export function settleConductorLine(pendingId: string, text: string): void {
  publish(resolveConductorLine(moduleTranscript, pendingId, text));
}
