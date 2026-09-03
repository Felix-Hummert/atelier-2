import { describe, expect, it } from "vitest";

import { encodeWaitAnswer } from "../../src/lib/waitAnswer";

describe("what a typed wait answer is sent as", () => {
  describe("a non-string schema (boolean, enum, object, or unclassified)", () => {
    it("sends plain words as the JSON string they plainly are", () => {
      expect(encodeWaitAnswer("ok", false)).toBe('"ok"');
      expect(encodeWaitAnswer("merge it, the review is green", false)).toBe(
        '"merge it, the review is green"'
      );
    });

    it("escapes what JSON must escape, so a quoted word still travels as one string", () => {
      expect(JSON.parse(encodeWaitAnswer('say "yes"', false))).toBe('say "yes"');
      expect(JSON.parse(encodeWaitAnswer("line\nline", false))).toBe("line\nline");
    });

    it("passes text that is already JSON through exactly as written", () => {
      expect(encodeWaitAnswer('{"verdict":"green"}', false)).toBe('{"verdict":"green"}');
      expect(encodeWaitAnswer("[1,2]", false)).toBe("[1,2]");
      expect(encodeWaitAnswer("42", false)).toBe("42");
      expect(encodeWaitAnswer("true", false)).toBe("true");
      expect(encodeWaitAnswer('"already quoted"', false)).toBe('"already quoted"');
    });

    it("drops the whitespace a textarea adds around what was meant", () => {
      expect(encodeWaitAnswer("  ok \n", false)).toBe('"ok"');
      expect(encodeWaitAnswer(' {"verdict":"green"} ', false)).toBe('{"verdict":"green"}');
    });
  });

  // #1091's dogfood defect: a nonempty_string wait (`{"type":"string"}`) reads
  // the artifact's own raw UTF-8 text as its value (`schemas_v3`), so the
  // composer must stop asking a person to spell `ok` as `"ok"`.
  describe('a string schema (WaitAnswerSchemaResourceV3.kind === "string")', () => {
    it("sends plain words verbatim, with no JSON-quoting layer", () => {
      expect(encodeWaitAnswer("ok", true)).toBe("ok");
      expect(encodeWaitAnswer("merge it, the review is green", true)).toBe(
        "merge it, the review is green"
      );
    });

    it("sends text that looks like JSON exactly as written, unreinterpreted", () => {
      expect(encodeWaitAnswer('{"verdict":"green"}', true)).toBe('{"verdict":"green"}');
      expect(encodeWaitAnswer('say "yes"', true)).toBe('say "yes"');
    });

    it("drops only the whitespace a textarea adds around what was meant", () => {
      expect(encodeWaitAnswer("  ok \n", true)).toBe("ok");
    });
  });
});
