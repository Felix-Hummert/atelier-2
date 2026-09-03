import { describe, expect, it } from "vitest";

import { matchesSearchTerm } from "../../src/lib/searchTerm";

describe("matchesSearchTerm", () => {
  it("matches a candidate case-insensitively, regardless of locale casing", () => {
    expect(matchesSearchTerm(["Deploy runner"], "DEPLOY")).toBe(true);
  });

  it("matches on any one of several candidates", () => {
    expect(matchesSearchTerm([null, "Preview door", "manual"], "door")).toBe(true);
  });

  it("does not match when no candidate contains the term", () => {
    expect(matchesSearchTerm(["Preview door"], "rotate")).toBe(false);
  });

  it("treats a blank or whitespace-only term as matching everything", () => {
    expect(matchesSearchTerm(["Preview door"], "")).toBe(true);
    expect(matchesSearchTerm(["Preview door"], "   ")).toBe(true);
  });

  it("ignores null candidates instead of matching or throwing on them", () => {
    expect(matchesSearchTerm([null, null], "door")).toBe(false);
  });
});
