import { describe, expect, it } from "vitest";

import { shortFingerprint } from "../../src/lib/fingerprint";

describe("how a long machine value is written where a person reads it", () => {
  it("keeps both ends of a digest, which is what a reader compares", () => {
    const digest = `${"a".repeat(8)}${"0".repeat(52)}9594`;
    expect(digest).toHaveLength(64);

    const shortened = shortFingerprint(digest);

    expect(shortened.startsWith(digest.slice(0, 8))).toBe(true);
    expect(shortened.endsWith("9594")).toBe(true);
    expect(shortened.length).toBeLessThan(digest.length);
  });

  it("leaves a speaking identifier exactly as written, however long it reads", () => {
    // A name a person chose is content, not a machine value to compare.
    expect(shortFingerprint("v3/seen-in-the-browser")).toBe("v3/seen-in-the-browser");
    expect(shortFingerprint("v3/two-agents")).toBe("v3/two-agents");
    expect(shortFingerprint("")).toBe("");
  });
});
