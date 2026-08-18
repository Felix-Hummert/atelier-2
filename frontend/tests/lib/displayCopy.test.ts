import { afterEach, describe, expect, it } from "vitest";

import { wrapDisplayCopy } from "../../src/lib/displayCopy";

afterEach(() => {
  window.history.replaceState(null, "", "/atelier");
});

describe("owned display copy under a pseudo-locale", () => {
  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): leaves owned English alone when the flag is off", () => {
    window.history.replaceState(null, "", "/atelier");
    expect(wrapDisplayCopy("Studio")).toBe("Studio");
  });

  it("proves(core-surfaces-render-owned-display-strings-under-a-pseudo-locale): lengthens an owned string when the flag is on", () => {
    window.history.replaceState(null, "", "/atelier?pseudo-locale=1");
    expect(wrapDisplayCopy("Studio")).toBe("[[[ Studio ]]]");
    expect(wrapDisplayCopy("Studio").length).toBeGreaterThan("Studio".length);
  });
});
