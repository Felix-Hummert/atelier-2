import { describe, expect, it } from "vitest";

import { observedWorkItemLabel } from "../../src/lib/catalogPageCopy";

describe("the catalog start-sheet copy", () => {
  it("places a last-observed title beside its tracker reference without inventing one", () => {
    expect(observedWorkItemLabel("#450", "Preview door")).toBe("#450 Preview door");
    expect(observedWorkItemLabel("#451", null)).toBe("#451");
  });
});
