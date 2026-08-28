import { describe, expect, it } from "vitest";

import { historyOutcome } from "../../src/lib/historyOutcome";
import {
  historyBreakdownOutcome,
  historyCodeReviewOutcome,
  historyFieldsShape,
  historyItemsShape,
  historyPageCopy,
  historyRefineOutcome
} from "../../src/lib/historyPageCopy";

const SECRET = "sk-live-should-never-appear";

describe("historyOutcome for known catalog workflows", () => {
  it("names a code-review by verdict, finding count, and highest severity", () => {
    const revise = historyOutcome(
      "code-review",
      JSON.stringify({
        verdict: "revise",
        findings: [
          { file: "a.ts", line: 1, severity: "low", text: SECRET },
          { file: "b.ts", line: 2, severity: "high", text: SECRET },
          { file: "c.ts", line: 3, severity: "medium", text: SECRET }
        ]
      })
    );
    expect(revise).toBe(
      historyCodeReviewOutcome(historyPageCopy.outcome.revise, 3, historyPageCopy.outcome.high)
    );
    expect(revise).not.toContain(SECRET);
    expect(revise).not.toContain("a.ts");

    expect(
      historyOutcome("code-review", JSON.stringify({ verdict: "approve", findings: [] }))
    ).toBe(historyPageCopy.outcome.approved);
  });

  it("names a refine by expectation lines and lenses, never the mirror", () => {
    const sentence = historyOutcome(
      "refine",
      JSON.stringify({
        mirror: `So habe ich dich verstanden. ${SECRET}`,
        rounds: 1,
        expectations: [
          { lens: "identity", sentence: SECRET },
          { lens: "identity", sentence: SECRET },
          { lens: "states", sentence: SECRET }
        ],
        lenses_without_lines: ["undo", "scale", "create_change_remove", "secrets_and_rights"],
        verdict: "complete"
      })
    );
    expect(sentence).toBe(historyRefineOutcome(3, 6));
    expect(sentence).not.toContain(SECRET);
  });

  it("names documentation-curation and plan-review by verdict only", () => {
    expect(
      historyOutcome(
        "documentation-curation",
        JSON.stringify({
          verdict: "approve",
          findings: [{ document: "docs/A.md", text: SECRET }]
        })
      )
    ).toBe(historyPageCopy.outcome.approved);

    expect(
      historyOutcome("plan-review", JSON.stringify({ verdict: "pass", risks: [{ text: SECRET }] }))
    ).toBe(historyPageCopy.outcome.pass);
  });

  it("names a breakdown by slice count and verdict", () => {
    const sentence = historyOutcome(
      "breakdown",
      JSON.stringify({
        slices: [{ title: SECRET }, { title: SECRET }],
        contradictions: [],
        verdict: "buildable"
      })
    );
    expect(sentence).toBe(historyBreakdownOutcome(2, historyPageCopy.outcome.buildable));
    expect(sentence).not.toContain(SECRET);
  });
});

describe("historyOutcome for an unknown workflow never carries content", () => {
  it("states the number of fields, never the field values", () => {
    const sentence = historyOutcome(
      "hello-atelier",
      JSON.stringify({ token: SECRET, password: "hunter2", note: "keep out" })
    );
    expect(sentence).toBe(historyFieldsShape(3));
    expect(sentence).not.toContain(SECRET);
    expect(sentence).not.toContain("hunter2");
    expect(sentence).not.toContain("keep out");
  });

  it("states the number of items for an array, and text for unstructured bytes", () => {
    expect(historyOutcome("unknown", JSON.stringify([SECRET, "two"]))).toBe(historyItemsShape(2));
    expect(historyOutcome("unknown", `plain ${SECRET}`)).toBe(historyPageCopy.outcome.text);
    expect(historyOutcome("unknown", `plain ${SECRET}`)).not.toContain(SECRET);
  });

  it("does not treat a code-review-shaped payload as a review when the workflow is unknown", () => {
    const sentence = historyOutcome(
      "custom-review",
      JSON.stringify({ verdict: "approve", findings: [{ text: SECRET, severity: "high" }] })
    );
    expect(sentence).toBe(historyFieldsShape(2));
    expect(sentence).not.toContain(SECRET);
    expect(sentence).not.toBe(historyPageCopy.outcome.approved);
  });
});
