import { describe, expect, it } from "vitest";

import { workflowStartCopy } from "../../src/lib/catalogPageCopy";
import { readRawOrderJson } from "../../src/lib/rawOrderJson";

describe("reading the start sheet's Raw JSON field", () => {
  it("accepts valid JSON of any shape", () => {
    expect(readRawOrderJson('{"diff": "text"}', workflowStartCopy.rawJsonWayOutBesideForm)).toEqual({
      ok: true
    });
    expect(readRawOrderJson("[1, 2, 3]", workflowStartCopy.rawJsonWayOutBesideForm)).toEqual({ ok: true });
    expect(
      readRawOrderJson('"a bare string is valid JSON too"', workflowStartCopy.rawJsonWayOutBesideForm)
    ).toEqual({ ok: true });
  });

  it("names the line and column of a syntax mistake, and the caller's way out (#438 Zeile 9, 11)", () => {
    const verdict = readRawOrderJson(
      '{\n  "diff": "text",\n  broken\n}',
      workflowStartCopy.rawJsonWayOutBesideForm
    );
    expect(verdict.ok).toBe(false);
    if (verdict.ok) throw new Error("expected a refusal");
    expect(verdict.reason).toContain("line 3");
    expect(verdict.reason).toContain(workflowStartCopy.rawJsonWayOutBesideForm);
  });

  it("refuses empty text as invalid JSON rather than an empty value", () => {
    expect(readRawOrderJson("", workflowStartCopy.rawJsonWayOutBesideForm).ok).toBe(false);
  });

  it("still refuses an unquoted-value syntax mistake and names the caller's way out (#1130 finding 7: the engine's own message shape for this input is not pinned)", () => {
    const verdict = readRawOrderJson('{"priority": high}', workflowStartCopy.rawJsonWayOutBesideForm);
    expect(verdict.ok).toBe(false);
    if (verdict.ok) throw new Error("expected a refusal");
    expect(verdict.reason).toContain(workflowStartCopy.rawJsonWayOutBesideForm);
  });

  it("carries whichever way out the caller names, pinning the raw_object-alone wording separately (#1130 finding 2)", () => {
    const verdict = readRawOrderJson('{"priority": high}', workflowStartCopy.rawJsonWayOutAlone);
    expect(verdict.ok).toBe(false);
    if (verdict.ok) throw new Error("expected a refusal");
    expect(verdict.reason).toContain(workflowStartCopy.rawJsonWayOutAlone);
    expect(verdict.reason).not.toContain(workflowStartCopy.rawJsonWayOutBesideForm);
  });
});
