import { describe, expect, it } from "vitest";

import { readRawOrderJson } from "../../src/lib/rawOrderJson";

describe("reading the start sheet's Raw JSON field", () => {
  it("accepts valid JSON of any shape", () => {
    expect(readRawOrderJson('{"diff": "text"}')).toEqual({ ok: true });
    expect(readRawOrderJson("[1, 2, 3]")).toEqual({ ok: true });
    expect(readRawOrderJson('"a bare string is valid JSON too"')).toEqual({ ok: true });
  });

  it("names the line and column of a syntax mistake, and a way out (#438 Zeile 9, 11)", () => {
    const verdict = readRawOrderJson('{\n  "diff": "text",\n  broken\n}');
    expect(verdict.ok).toBe(false);
    if (verdict.ok) throw new Error("expected a refusal");
    expect(verdict.reason).toContain("line 3");
    expect(verdict.reason).toContain("Fix the JSON, or clear this field and fill the form above instead.");
  });

  it("refuses empty text as invalid JSON rather than an empty value", () => {
    expect(readRawOrderJson("").ok).toBe(false);
  });

  it("still refuses an unquoted-value syntax mistake and names the way out (#1130 finding 7: the engine's own message shape for this input is not pinned)", () => {
    const verdict = readRawOrderJson('{"priority": high}');
    expect(verdict.ok).toBe(false);
    if (verdict.ok) throw new Error("expected a refusal");
    expect(verdict.reason).toContain("Fix the JSON, or clear this field and fill the form above instead.");
  });
});
