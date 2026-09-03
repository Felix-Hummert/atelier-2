import { describe, expect, it } from "vitest";

import { trackerItemHref, trackerItemLabel } from "../../src/lib/trackerItem";

describe("trackerItemLabel uses the adapter grammar", () => {
  it.each([
    ["gh:567", "#567"],
    ["gl:12", "!12"],
    ["other:9", "other:9"]
  ])("%s reads %s", (reference, label) => {
    expect(trackerItemLabel(reference)).toBe(label);
  });
});

describe("trackerItemHref names the issue when the source can form one", () => {
  const github = { source_kind: "github", source_address: "FlexOr2/atelier-2@main" };

  it("links a GitHub issue from the connected repository", () => {
    expect(trackerItemHref("gh:567", github)).toBe(
      "https://github.com/FlexOr2/atelier-2/issues/567"
    );
  });

  it("does not invent a URL without a source, or for another kind", () => {
    expect(trackerItemHref("gh:567", null)).toBeNull();
    expect(trackerItemHref("gl:12", github)).toBeNull();
    expect(trackerItemHref("gh:567", { source_kind: "gitlab", source_address: "g/g@main" })).toBeNull();
  });
});
