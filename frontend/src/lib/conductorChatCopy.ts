/**
 * Copy for the Workbench conversation while a conductor IS connected.
 *
 * `workbenchPageCopy` keeps the surface's fixed strings and everything the
 * house says while no conductor is connected; this module owns the strings the
 * conductor episodes add — what the composer promises, what a line says while
 * an episode runs, and how a failed episode names itself. Same convention as
 * every `*Copy` owner: one source per string, provable by `?pseudo-locale=1`.
 */
export const conductorChatCopy = {
  emptyDescription:
    "The conductor is listening. Each message becomes one bounded run, and its reply returns to this conversation.",
  composerHint:
    "The conductor is connected: each message becomes one bounded run whose reply returns here.",
  /** The pinned placeholder a sent message holds until its episode ends. */
  reading: "The conductor is reading your message…",
  startRefused: "The conductor could not start a run for this message:",
  episodeFailed: "The conductor's run ended without an answer:",
  replyUnreadable: "The conductor's run finished, but its reply could not be read.",
  streamLost:
    "The reply could not be followed here. Open the run to read how it ended.",
  /** Leads the run ids a reply set in motion, exactly as the episode reported them. */
  startedRuns: "Started:",
  openEpisode: "See the run",
  /**
   * The composer hint when the connection read itself failed: neither
   * "connected" nor "not connected" would be true, so the failure says so.
   */
  connectionUnknown:
    "Whether a conductor is connected could not be read. Reload to try again."
} as const;
