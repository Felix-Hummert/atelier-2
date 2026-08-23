/**
 * Copy the Chat surface renders (mockup v5 §01): the conversation the operator
 * types into, and the one honest answer the house can give today.
 *
 * The conductor that would read a message and start work is Vision #7 and is
 * not built. The composer is real all the same — what is typed is sent, kept
 * and shown — and the reply names the missing door rather than inventing an
 * answer or pretending a run began.
 *
 * One owner for this screen's strings, the same convention `studioPageCopy`
 * and `historyPageCopy` already hold to, so `?pseudo-locale=1`
 * (`wrapDisplayCopy`) can prove every string here has a source instead of a
 * second hardcoded copy inline in the page.
 */
export const chatPageCopy = {
  eyebrow: "Atelier",
  title: "Chat",
  transcriptLabel: "Conversation",
  emptyTitle: "Nothing said yet",
  emptyDescription:
    "A conductor that turns what you say into runs is Vision #7 and is not built — until it is, work starts in Workflows.",
  emptyNext: "Open Workflows",
  youLabel: "You",
  houseLabel: "Atelier",
  composerLabel: "Message",
  send: "Send",
  /**
   * The reply every sent message gets while Vision #7 is unbuilt. It says
   * three true things: nothing was started, the message was not thrown away,
   * and which vision owns the door that is missing.
   */
  conductorAbsent:
    "No conductor is connected yet, so nothing was started. Your message is kept in this conversation for as long as this page is open. Vision #7 owns that door.",
  conductorAbsentSource: "#7"
} as const;
