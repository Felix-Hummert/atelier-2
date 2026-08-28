import { describe, expect, it } from "vitest";

import {
  trackerItemHref,
  trackerItemLabel,
  workItemReferenceFromJob,
  workItemReferenceFromOrderDocument
} from "../../src/lib/trackerItem";

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

describe("workItemReferenceFromOrderDocument reads only the house-schema reference", () => {
  const document = {
    body: "this body is never a title",
    change_marker: "etag",
    digest: "a".repeat(64),
    kind: "issue",
    observed_at: "2026-08-26T09:15:00Z",
    reference: "gh:567"
  };

  it("returns the reference and never the body", () => {
    expect(workItemReferenceFromOrderDocument(document)).toBe("gh:567");
  });

  it("refuses a guessed object that is not the closed house schema", () => {
    expect(workItemReferenceFromOrderDocument({ reference: "gh:567", title: "guessed" })).toBeNull();
    expect(workItemReferenceFromOrderDocument({ ...document, extra: "x" })).toBeNull();
  });
});

describe("workItemReferenceFromJob finds the order document in the composed job", () => {
  it("reads the reference from an order block and ignores other JSON", () => {
    const job = [
      "Do the one thing.",
      "",
      '{"answer":"not a work item"}',
      "",
      "--- order: work_item ---",
      JSON.stringify({
        body: "SECRET TITLE FROM BODY",
        change_marker: "etag",
        digest: "a".repeat(64),
        kind: "issue",
        observed_at: "2026-08-26T09:15:00Z",
        reference: "gh:450"
      })
    ].join("\n\n");
    expect(workItemReferenceFromJob(job)).toBe("gh:450");
  });

  it("names none when the job carries no work-item document", () => {
    expect(workItemReferenceFromJob("Do the one thing.\n\nnot json")).toBeNull();
  });
});
