/**
 * Copy the Workbench surface renders: the pinned decisions that need the
 * operator, the conversation typed into the composer, and the one honest thing
 * the house can say while no conductor reads those words yet.
 *
 * The Workbench is the chat surface grown into a workshop (issue #580): a
 * composer and a fixed "Needs you" region that pins every open decision until
 * it is answered, so a request can never scroll away in a growing stream (the
 * failure mode that ruling names). The conductor that would read a message and
 * start work is not built yet; the composer is real all the same, and says so
 * in one honest sentence rather than a button that duplicates a door.
 *
 * Which vision or issue owns the conductor gap is repository bookkeeping, not
 * operator-facing copy (Adressaten-Regel, operator ruling 23.08.) — no board or
 * issue number appears in any string below.
 *
 * One owner for this screen's strings, the same convention `studioPageCopy` and
 * `runPageCopy` already hold to, so `?pseudo-locale=1` (`wrapDisplayCopy`) can
 * prove every string here has a source instead of a second hardcoded copy
 * inline in the page.
 */
export const workbenchPageCopy = {
  title: "Workbench",

  /** The pinned "Needs you" region: the decisions that must not scroll away. */
  needsYouTitle: "Needs you",
  needsYouNone: "Nothing needs you right now.",
  /** Names who is asking, so a pinned decision is never a question from nowhere. */
  waitingFrom: "is waiting for you",
  openTheRun: "Open the run",
  openTheRunForStory: "Open the run for the whole story",

  transcriptLabel: "Conversation",
  emptyTitle: "Nothing said yet",
  emptyDescription:
    "The conductor that turns what you say into runs is not built yet. Until then, start work from Workflows in the rail.",
  youLabel: "You",
  houseLabel: "Atelier",
  composerLabel: "Message",
  send: "Send",
  /**
   * The one honest sentence the composer carries while no conductor is
   * connected (HEART, "The ear"): it says plainly that words are not yet turned
   * into runs, without a second button that duplicates a door.
   */
  composerHint: "No conductor is connected yet, so your words are kept here but start nothing.",
  /**
   * The reply every sent message gets while no conductor is connected. The
   * standing composer hint already carries "no conductor is connected yet"
   * (HEART, "The ear"), so the reply drops that duplicated lead and keeps only
   * its two unique truths: nothing was started, and the message was not thrown
   * away. "Until you reload" is the conversation's real boundary: it survives
   * in-app rail navigation (the module that owns it outlives the page
   * component) but not a reload.
   */
  conductorAbsent:
    "Nothing was started. Your message is kept in this conversation until you reload the page."
} as const;
