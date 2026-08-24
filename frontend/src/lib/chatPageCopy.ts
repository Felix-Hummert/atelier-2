/**
 * Copy the Chat surface renders (mockup v5 §01): the conversation the operator
 * types into, and the one honest answer the house can give today.
 *
 * The conductor that would read a message and start work is not built yet.
 * The composer is real all the same — what is typed is sent, kept and shown
 * — and the reply names the gap honestly rather than inventing an answer or
 * pretending a run began. Which vision or issue owns that gap is repository
 * bookkeeping, not operator-facing copy (Adressaten-Regel, operator ruling
 * 23.08.) — it stays out of every string below.
 *
 * One owner for this screen's strings, the same convention `studioPageCopy`
 * and `historyPageCopy` already hold to, so `?pseudo-locale=1`
 * (`wrapDisplayCopy`) can prove every string here has a source instead of a
 * second hardcoded copy inline in the page.
 */
export const chatPageCopy = {
  title: "Chat",
  transcriptLabel: "Conversation",
  emptyTitle: "Nothing said yet",
  emptyDescription:
    "The conductor that turns what you say into runs is not built yet. Until then, start work from Workflows in the rail.",
  youLabel: "You",
  houseLabel: "Atelier",
  composerLabel: "Message",
  send: "Send",
  /**
   * The reply every sent message gets while no conductor is connected. It
   * says two true things: nothing was started, and the message was not
   * thrown away. "Until you reload" is the conversation's real boundary: it
   * survives in-app rail navigation (the module that owns it outlives the
   * page component) but not a reload.
   */
  conductorAbsent:
    "No conductor is connected yet, so nothing was started. Your message is kept in this conversation until you reload the page."
} as const;
