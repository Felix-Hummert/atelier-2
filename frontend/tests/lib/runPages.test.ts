import { describe, expect, it, vi } from "vitest";

import type { RunPage } from "../../src/api/client";
import { readEveryRun } from "../../src/lib/runPages";
import { runRow, startedRun } from "../support/runV3";

function page(items: RunPage["items"], next_after: string | null): RunPage {
  return { items, next_after };
}

/**
 * `readEveryRun`'s starting cursor (#1109 delta MEDIUM): History's Retry
 * depends on this to resume the one list that stopped rather than restart
 * every list from its first page.
 */
describe("readEveryRun resuming from a starting cursor", () => {
  it("reads its first page from the given cursor instead of from the start", async () => {
    const listRuns = vi.fn(async () => page([], null));

    await readEveryRun(listRuns, "run1.stopped-here");

    expect(listRuns).toHaveBeenCalledTimes(1);
    expect(listRuns).toHaveBeenCalledWith("run1.stopped-here");
  });

  it("reads from the start when no cursor is given", async () => {
    const listRuns = vi.fn(async () => page([], null));

    await readEveryRun(listRuns);

    expect(listRuns).toHaveBeenCalledTimes(1);
    expect(listRuns).toHaveBeenCalledWith(undefined);
  });

  it("returns the rows a resumed read collects, complete once its own cursor ends", async () => {
    const row = runRow(startedRun());
    const listRuns = vi.fn(async () => page([row], null));

    const reading = await readEveryRun(listRuns, "run1.stopped-here");

    expect(reading).toEqual({ complete: true, runs: [row] });
  });

  it("names its own starting cursor as where it stopped, when the resumed read fails immediately", async () => {
    const listRuns = vi.fn(async () => {
      throw new Error("still unavailable");
    });

    const reading = await readEveryRun(listRuns, "run1.stopped-here");

    expect(reading).toEqual({
      complete: false,
      runs: [],
      unreadable: "still unavailable",
      cursor: "run1.stopped-here"
    });
  });

  it("throws, rather than reporting a stopped reading, when a fresh read's first page fails", async () => {
    const listRuns = vi.fn(async () => {
      throw new Error("unavailable from the start");
    });

    await expect(readEveryRun(listRuns)).rejects.toThrow("unavailable from the start");
  });
});
