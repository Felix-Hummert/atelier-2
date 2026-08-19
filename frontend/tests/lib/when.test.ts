import { describe, expect, it } from "vitest";

import { ageLabel, exactLocal } from "../../src/lib/when";

const start = "2026-08-18T15:00:00Z";
const now = new Date("2026-08-18T15:05:00Z");

describe("when", () => {
  it("names age where the question is how long", () => {
    expect(ageLabel(start, now, "ago")).toBe("5 min ago");
    expect(ageLabel(start, now, "for")).toBe("for 5 min");
    expect(ageLabel(start, now, "duration", "2026-08-18T15:02:00Z")).toBe("2 min");
    expect(ageLabel(start, now, "duration", "2026-08-18T15:00:12Z")).toBe("12 s");
  });

  it("keeps the exact stamp behind the surface clock", () => {
    expect(exactLocal(start)).toContain("2026");
  });
});
