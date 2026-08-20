import { describe, expect, it } from "vitest";

import {
  beginRead,
  confirmRead,
  failRead,
  retainedRead,
  updateConfirmed
} from "../../src/lib/readResource";

type ScenarioFailure =
  | { kind: "unavailable"; title: string }
  | { kind: "incomplete"; title: string };

describe("one retained read owner", () => {
  it("keeps confirmed truth through loading and failure", () => {
    const first = beginRead(retainedRead<{ id: string }, ScenarioFailure>());
    const confirmed = confirmRead(first.read, first.generation, { id: "run" });
    const refresh = beginRead(confirmed);
    const failed = failRead(
      refresh.read,
      refresh.generation,
      { kind: "unavailable", title: "Project runs unavailable" }
    );

    expect(refresh.read.confirmed).toEqual({ id: "run" });
    expect(failed.confirmed).toEqual({ id: "run" });
    expect(failed.request).toEqual({
      state: "failed",
      failure: {
        kind: "unavailable",
        title: "Project runs unavailable"
      }
    });
  });

  it("fences an older success and failure behind the current generation", () => {
    const older = beginRead(retainedRead<string, ScenarioFailure>());
    const newer = beginRead(older.read);
    const confirmed = confirmRead(newer.read, newer.generation, "new truth");

    expect(confirmRead(confirmed, older.generation, "old truth")).toBe(confirmed);
    expect(
      failRead(confirmed, older.generation, {
        kind: "incomplete",
        title: "Studio runs incomplete"
      })
    ).toBe(confirmed);
    expect(confirmed.confirmed).toBe("new truth");
  });

  it("merges newer pushed truth without completing an in-flight read", () => {
    const first = beginRead(retainedRead<number, ScenarioFailure>());
    const confirmed = confirmRead(first.read, first.generation, 1);
    const loading = beginRead(confirmed);
    const pushed = updateConfirmed(loading.read, 2);

    expect(pushed.confirmed).toBe(2);
    expect(pushed.request.state).toBe("loading");
  });
});
