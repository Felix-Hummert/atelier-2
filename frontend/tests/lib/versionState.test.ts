import { get } from "svelte/store";
import { describe, expect, it } from "vitest";

import {
  loadedVersion,
  newVersionAvailable,
  noteObservedVersion,
  recordLoadedVersion
} from "../../src/lib/versionState";

const LOADED = { commit: "a".repeat(40), deployedAt: "2026-08-31T08:00:00Z" };

describe("the loaded serve version, compared against later health answers (#1100)", () => {
  it("names the version the page loaded with", () => {
    recordLoadedVersion(LOADED);

    expect(get(loadedVersion)).toEqual(LOADED);
    expect(get(newVersionAvailable)).toBe(false);
  });

  it("flags a mismatch only once a different commit is observed", () => {
    recordLoadedVersion(LOADED);

    noteObservedVersion(LOADED);
    expect(get(newVersionAvailable)).toBe(false);

    noteObservedVersion({ commit: "b".repeat(40), deployedAt: "2026-09-01T08:00:00Z" });
    expect(get(newVersionAvailable)).toBe(true);
  });

  it("resets the mismatch on the next fresh load", () => {
    recordLoadedVersion(LOADED);
    noteObservedVersion({ commit: "b".repeat(40), deployedAt: "2026-09-01T08:00:00Z" });
    expect(get(newVersionAvailable)).toBe(true);

    recordLoadedVersion({ commit: "b".repeat(40), deployedAt: "2026-09-01T08:00:00Z" });

    expect(get(newVersionAvailable)).toBe(false);
  });
});
