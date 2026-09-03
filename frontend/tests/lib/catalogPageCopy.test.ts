import { describe, expect, it } from "vitest";

import { observedWorkItemLabel, workflowDetailCopy } from "../../src/lib/catalogPageCopy";

describe("the catalog start-sheet copy", () => {
  it("places a last-observed title beside its tracker reference without inventing one", () => {
    expect(observedWorkItemLabel("#450", "Preview door")).toBe("#450 Preview door");
    expect(observedWorkItemLabel("#451", null)).toBe("#451");
  });
});

describe("a revision's source fact (#1077, #1112)", () => {
  it("shortens the commit to git's own 8-hex prefix, keeping the full hash as the hover title", () => {
    const commit = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2";

    expect(workflowDetailCopy.sourceFact(commit, "flows/build.yaml")).toEqual({
      label: "a1b2c3d4 · flows/build.yaml",
      title: commit
    });
  });
});
