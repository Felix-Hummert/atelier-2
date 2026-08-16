/**
 * What this installation's one project is called on screen.
 *
 * The wire serves no name for a run, an agent, or this installation, so this is
 * client-owned wording and never a stored datum. An invented project name would
 * read like a saved one and be none. It lives in one place because the studio
 * card, the project level and the run's trail must never disagree about what the
 * operator is looking at.
 *
 * A workflow revision does carry a name since #146, which is why the picker can
 * offer one. Nothing above a run can, which is why rule 5 stays open at #22.
 *
 * When #133 gives a project a backend identity, this constant is replaced by
 * that name, and every screen that reads it follows.
 */
export const THE_ONE_PROJECT = "This workshop";
