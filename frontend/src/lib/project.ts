/**
 * What this installation's one project is called on screen.
 *
 * The wire serves no name field — not for a run, a workflow, an agent, or the
 * installation — so this is client-owned wording and never a stored datum. An
 * invented project name would read like a saved one and be none. It lives in
 * one place because the studio card and the project level must never disagree
 * about what the operator is looking at.
 *
 * When #133 gives a project a backend identity, this constant is replaced by
 * that name, and every screen that reads it follows.
 */
export const THE_ONE_PROJECT = "This workshop";
