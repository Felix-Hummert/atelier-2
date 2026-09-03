import {
  decodeStreamFrame,
  isRunProjectionCorrupt,
  isStreamFailure,
  type CockpitApi,
  type RunEvent,
  type RunV3
} from "../api/client";
import { conductorChatCopy } from "./conductorChatCopy";
import { decodeUtf8Base64 } from "./exactBytes";
import { createRunId, startMutation, type MutationJournal } from "./mutationJournal";
import { CONDUCTOR_CONVERSATION_RUN_STORAGE_KEY } from "./storageKeys";
import { deliverWaitAnswer, prepareWaitAnswer, type WaitAnswerOutcome } from "./waitAnswerDelivery";
import type { ConductorConnection } from "./conductorEpisode";

/**
 * One line of the conversation. It carries no run of its own: the whole
 * conversation is one run (#658), and the page links to it once.
 */
export type ConductorMessage = {
  id: string;
  speaker: "you" | "house";
  text: string;
};

export type ConductorTranscript = {
  messages: readonly ConductorMessage[];
  seenCursors: ReadonlySet<string>;
};

const CONDUCTOR_WAIT_NODE_ID = "next_message";
const CONDUCTOR_AGENT_NODE_ID = "conduct";
export const conductorConversationCopy = {
  emptyDescription: "The conductor is ready for the first message.",
  composerHint: "The conductor is listening. Your next message begins the conversation.",
  /** Shown once at least one round has actually landed in the transcript: the
   * "begins" wording above only holds for the conversation's own first
   * message, before anything has been exchanged. */
  composerHintOngoing: "The conductor is listening. Your next message continues this conversation.",
  /** Shown once a run ended without completing (failed or cancelled): the
   * composer stays open, but a new message starts a new conversation rather
   * than continuing the one that ended. */
  endedHint: "That conversation ended. Your next message starts a new one.",
  complete: "Conversation completed"
} as const;

/** The current run reference lets a reload ask its durable SSE history again. */
export function rememberedConductorRun(storage: Storage): string | null {
  return storage.getItem(CONDUCTOR_CONVERSATION_RUN_STORAGE_KEY);
}

export function rememberConductorRun(storage: Storage, publicRunReference: string): void {
  storage.setItem(CONDUCTOR_CONVERSATION_RUN_STORAGE_KEY, publicRunReference);
}

export function emptyConductorTranscript(): ConductorTranscript {
  return { messages: [], seenCursors: new Set() };
}

/**
 * A durable run stream is the conversation's source of truth. The cursor
 * makes replays and EventSource redelivery harmless, including on reload when
 * the server sends the entire history again.
 */
export function reduceConductorEvent(
  transcript: ConductorTranscript,
  event: RunEvent
): ConductorTranscript {
  if (transcript.seenCursors.has(event.cursor)) return transcript;
  const seenCursors = new Set(transcript.seenCursors);
  seenCursors.add(event.cursor);
  const message = messageFromEvent(event);
  return {
    messages: message === null ? transcript.messages : [...transcript.messages, message],
    seenCursors
  };
}

export function decodeConductorEvent(rawData: string): RunEvent | null {
  try {
    const frame = decodeStreamFrame(JSON.parse(rawData));
    return isStreamFailure(frame) || isRunProjectionCorrupt(frame) ? null : frame;
  } catch {
    return null;
  }
}

function messageFromEvent(event: RunEvent): ConductorMessage | null {
  if (event.event === "WAIT_ANSWERED" && event.node_id === CONDUCTOR_WAIT_NODE_ID) {
    return {
      id: `operator-${event.cursor}`,
      speaker: "you",
      text: readableWaitAnswer(event.answer_base64)
    };
  }
  if (event.event === "AGENT_COMPLETED" && event.node_id === CONDUCTOR_AGENT_NODE_ID) {
    return {
      id: `conductor-${event.cursor}`,
      speaker: "house",
      text: readableReport(event.output_base64)
    };
  }
  if (event.event === "AGENT_FAILED" && event.node_id === CONDUCTOR_AGENT_NODE_ID) {
    return {
      id: `conductor-${event.cursor}`,
      speaker: "house",
      text: conductorChatCopy.episodeFailed
    };
  }
  return null;
}

function readableWaitAnswer(answerBase64: string): string {
  const decoded = decodeUtf8Base64(answerBase64);
  if (decoded === null) return conductorChatCopy.replyUnreadable;
  try {
    const answer: unknown = JSON.parse(decoded);
    return typeof answer === "string" ? answer : JSON.stringify(answer);
  } catch {
    return decoded;
  }
}

function readableReport(outputBase64: string): string {
  const decoded = decodeUtf8Base64(outputBase64);
  if (decoded === null) return conductorChatCopy.replyUnreadable;
  try {
    const report: unknown = JSON.parse(decoded);
    if (
      report === null ||
      typeof report !== "object" ||
      !Object.hasOwn(report, "answer") ||
      typeof (report as { answer: unknown }).answer !== "string"
    ) {
      return conductorChatCopy.replyUnreadable;
    }
    return (report as { answer: string }).answer;
  } catch {
    return conductorChatCopy.replyUnreadable;
  }
}

/** Starts the zero-input conductor loop; its first message waits for `next_message`. */
export async function startConductorConversation(
  cockpitApi: CockpitApi,
  connection: ConductorConnection
): Promise<RunV3> {
  return (
    await cockpitApi.start(
    startMutation(createRunId(), connection.workflowRevisionHash, [
      {
        role: connection.role,
        agent_configuration_revision_hash: connection.agentConfigurationRevisionHash
      }
    ], [])
    )
  ).value;
}

/** Every later operator message is the current round's ordinary wait answer. */
export async function answerConductorWait(
  cockpitApi: CockpitApi,
  mutationJournal: MutationJournal,
  run: RunV3,
  typed: string
): Promise<WaitAnswerOutcome> {
  if (run.current_node_id !== CONDUCTOR_WAIT_NODE_ID) {
    throw new Error("The conductor has not reached its message wait.");
  }
  const mutation = await prepareWaitAnswer(
    mutationJournal,
    run.public_run_reference,
    run.workflow_revision_hash,
    run.current_node_id,
    run.current_node_execution_id,
    typed
  );
  return deliverWaitAnswer(cockpitApi, mutationJournal, mutation);
}
