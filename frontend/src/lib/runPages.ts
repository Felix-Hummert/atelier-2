import type { Run, RunPage } from "../api/client";

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
  | { complete: true; runs: Run[] }
  | { complete: false; runs: Run[]; unreadable: string };

export async function readEveryRun(
  listRuns: (after?: string) => Promise<RunPage>
): Promise<RunReading> {
  const runs: Run[] = [];
  const collected = new Set<string>();
  const followed = new Set<string>();
  let after: string | undefined;
  for (;;) {
    let page: RunPage;
    try {
      page = await listRuns(after);
    } catch (error) {
      if (after === undefined) {
        throw error;
      }
      return { complete: false, runs, unreadable: failureText(error) };
    }
    for (const run of page.items) {
      if (!collected.has(run.public_run_reference)) {
        collected.add(run.public_run_reference);
        runs.push(run);
      }
    }
    if (page.next_after === null) {
      return { complete: true, runs };
    }
    if (followed.has(page.next_after)) {
      return {
        complete: false,
        runs,
        unreadable: "the durable list answered with a cursor it had already given"
      };
    }
    followed.add(page.next_after);
    after = page.next_after;
  }
}

function failureText(error: unknown): string {
  return error instanceof Error ? error.message : "a later page could not be read";
}
