import type {
  AgentConfigurationRevisionListItem,
  AgentConfigurationRevisionPage,
  AuthProfileRevision,
  AuthProfileRevisionPage,
  RunListRow,
  RunPage,
  WorkflowRevisionPage,
  WorkflowRevisionSummary
} from "../api/client";
import { runListRowReference } from "./runList";

/**
 * Every run the durable list holds, read page by page.
 *
 * The route serves a cursor and takes it back, so a client that reads one page
 * shows a part as the whole. This reads until the cursor ends -- with no page
 * cap, because any cap would be the same silent truncation one page later, and
 * the number of runs is not bounded by anything this client knows.
 *
 * The one bound is a correctness invariant rather than a guess: a cursor must
 * not repeat. Ordering cannot be checked here -- the cursor is an encoded
 * public reference while the durable order is over the raw run id -- but a
 * cursor that comes back is a durable defect, and a client must say so instead
 * of spinning on it.
 *
 * A run already collected is not collected again, for the same reason: the
 * defect that repeats a cursor repeats its rows, and a duplicated run would
 * take the surface down instead of letting it report what went wrong.
 */
export type RunReading =
  | { complete: true; runs: RunListRow[] }
  | { complete: false; runs: RunListRow[]; unreadable: string; cursor: string };

export type RevisionReading =
  | { complete: true; revisions: WorkflowRevisionSummary[] }
  | { complete: false; revisions: WorkflowRevisionSummary[]; unreadable: string };

export type AgentConfigurationReading =
  | { complete: true; configurations: AgentConfigurationRevisionListItem[] }
  | {
      complete: false;
      configurations: AgentConfigurationRevisionListItem[];
      unreadable: string;
    };

export type AuthProfileReading =
  | { complete: true; profiles: AuthProfileRevision[] }
  | { complete: false; profiles: AuthProfileRevision[]; unreadable: string };

/**
 * `after` resumes a stopped read instead of restarting from its first page
 * (#1109 delta MEDIUM): History's Retry passes the cursor a stopped read
 * named, so a retried list reads only what it is still missing rather than
 * discarding the pages it already confirmed.
 */
export async function readEveryRun(
  listRuns: (after?: string) => Promise<RunPage>,
  after?: string
): Promise<RunReading> {
  const read = await readEveryPage(
    async (nextAfter) => {
      const page = await listRuns(nextAfter);
      return { items: page.items, next: page.next_after };
    },
    runListRowReference,
    after
  );
  return read.complete
    ? { complete: true, runs: read.items }
    : { complete: false, runs: read.items, unreadable: read.unreadable, cursor: read.cursor };
}

/** The saved workflows a picker may offer, read the same way and for the same reason. */
export async function readEveryRevision(
  listRevisions: (after?: string) => Promise<WorkflowRevisionPage>
): Promise<RevisionReading> {
  const read = await readEveryPage(
    async (after) => {
      const page = await listRevisions(after);
      return { items: page.items, next: page.next_after_revision_hash };
    },
    (revision) => revision.workflow_revision_hash
  );
  return read.complete
    ? { complete: true, revisions: read.items }
    : { complete: false, revisions: read.items, unreadable: read.unreadable };
}

/** The published agent configurations a binding picker may offer. */
export async function readEveryAgentConfiguration(
  listConfigurations: (after?: string) => Promise<AgentConfigurationRevisionPage>
): Promise<AgentConfigurationReading> {
  const read = await readEveryPage(
    async (after) => {
      const page = await listConfigurations(after);
      return { items: page.items, next: page.next_after_revision_hash };
    },
    (configuration) => configuration.agent_configuration_revision_hash
  );
  return read.complete
    ? { complete: true, configurations: read.items }
    : { complete: false, configurations: read.items, unreadable: read.unreadable };
}

/** Every published account identity a model picker names beside its exact model. */
export async function readEveryAuthProfile(
  listProfiles: (after?: string) => Promise<AuthProfileRevisionPage>
): Promise<AuthProfileReading> {
  const read = await readEveryPage(
    async (after) => {
      const page = await listProfiles(after);
      return { items: page.items, next: page.next_after_revision_hash };
    },
    (profile) => profile.auth_profile_revision_hash
  );
  return read.complete
    ? { complete: true, profiles: read.items }
    : { complete: false, profiles: read.items, unreadable: read.unreadable };
}

type PageReading<Item> =
  | { complete: true; items: Item[] }
  | { complete: false; items: Item[]; unreadable: string; cursor: string };

async function readEveryPage<Item>(
  readPage: (after?: string) => Promise<{ items: readonly Item[]; next: string | null }>,
  identityOf: (item: Item) => string,
  startingAfter?: string
): Promise<PageReading<Item>> {
  const items: Item[] = [];
  const collected = new Set<string>();
  const followed = new Set<string>();
  let after: string | undefined = startingAfter;
  for (;;) {
    let page: { items: readonly Item[]; next: string | null };
    try {
      page = await readPage(after);
    } catch (error) {
      if (after === undefined) {
        throw error;
      }
      return { complete: false, items, unreadable: failureText(error), cursor: after };
    }
    for (const item of page.items) {
      const identity = identityOf(item);
      if (!collected.has(identity)) {
        collected.add(identity);
        items.push(item);
      }
    }
    if (page.next === null) {
      return { complete: true, items };
    }
    if (followed.has(page.next)) {
      return {
        complete: false,
        items,
        unreadable: "the durable list answered with a cursor it had already given",
        cursor: page.next
      };
    }
    followed.add(page.next);
    after = page.next;
  }
}

function failureText(error: unknown): string {
  return error instanceof Error ? error.message : "a later page could not be read";
}

/**
 * The run header names the workflow, never the raw run id (Constitution law 8).
 *
 * The run id still anchors the page beside the title, through the same
 * ProofAnchor pattern every other identifier on a run page already uses.
 */
export const runHeaderCopy = {
  runIdLabel: "Run id",
  sealsRunId: "the name this run answers to when the store is asked for it"
} as const;
