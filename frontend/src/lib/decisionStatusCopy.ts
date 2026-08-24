/**
 * The status words a decision card speaks once an answer is in flight: what the
 * heading says while the send travels, when it is stored but not yet acted on,
 * and when it could not be confirmed; the two alert labels for an uncertain or
 * failed send; and the label over the exact value that was sent.
 *
 * One owner so renaming a status word ("Answer uncertain") happens once, not
 * once per card (#611): the wait-answer cards each render this identical
 * vocabulary in their own markup, and the reconciliation card shares the
 * uncertain-send label. Only the words live here -- each card keeps its own
 * markup and its own state logic.
 */
export const decisionStatusCopy = {
  sending: "Sending answer",
  pending: "Answer pending",
  uncertain: "Answer uncertain",
  sendUncertain: "Send uncertain",
  sendFailed: "Send failed",
  exactAnswer: "Exact answer"
} as const;
