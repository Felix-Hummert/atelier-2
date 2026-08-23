import { describe, expect, it } from "vitest";

import { encodeWaitAnswer } from "../../src/lib/waitAnswer";

describe("what a typed wait answer is sent as", () => {
  it("sends plain words as the JSON string they plainly are", () => {
    expect(encodeWaitAnswer("ok")).toBe('"ok"');
    expect(encodeWaitAnswer("merge it, the review is green")).toBe(
      '"merge it, the review is green"'
    );
  });

  it("escapes what JSON must escape, so a quoted word still travels as one string", () => {
    expect(JSON.parse(encodeWaitAnswer('say "yes"'))).toBe('say "yes"');
    expect(JSON.parse(encodeWaitAnswer("line\nline"))).toBe("line\nline");
  });

  it("passes text that is already JSON through exactly as written", () => {
    expect(encodeWaitAnswer('{"verdict":"green"}')).toBe('{"verdict":"green"}');
    expect(encodeWaitAnswer("[1,2]")).toBe("[1,2]");
    expect(encodeWaitAnswer("42")).toBe("42");
    expect(encodeWaitAnswer("true")).toBe("true");
    expect(encodeWaitAnswer('"already quoted"')).toBe('"already quoted"');
  });

  it("drops the whitespace a textarea adds around what was meant", () => {
    expect(encodeWaitAnswer("  ok \n")).toBe('"ok"');
    expect(encodeWaitAnswer(' {"verdict":"green"} ')).toBe('{"verdict":"green"}');
  });
});
