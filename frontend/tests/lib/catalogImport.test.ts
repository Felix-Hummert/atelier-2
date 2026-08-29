import { describe, expect, it } from "vitest";

import {
  IMPORT_SHEET_KINDS,
  importKindLabel,
  importMistakeSentence,
  importSheetCanDeclare,
  importSheetReport
} from "../../src/lib/catalogImport";
import { catalogPageCopy } from "../../src/lib/catalogPageCopy";

describe("the import sheet's report, as distinct from the person's decision", () => {
  it("reports a recognized agent by glyph, count, and authored name, never as a Kind chip", () => {
    const report = importSheetReport(
      {
        outcome: "agent_definition",
        name: "walker",
        description: "Walks the surface.",
        provider_id: "anthropic"
      },
      "walker.md"
    );

    expect(report).toEqual({
      glyph: "◯",
      count: catalogPageCopy.oneAgent,
      name: "walker"
    });
    expect(importKindLabel("agent")).toBe(catalogPageCopy.kindAgent);
    expect(report.count).not.toBe(importKindLabel("agent"));
    expect(report.name).not.toBe(importKindLabel("agent"));
  });

  it("reports an uncertain file as a file, not a kind, so the chips stay the decision", () => {
    const report = importSheetReport(
      {
        outcome: "unrecognized",
        refusals: [
          { kind: "workflow", expected: "format_version", refused_because: "missing" }
        ]
      },
      "notes.md"
    );

    expect(report).toEqual({
      glyph: "·",
      count: catalogPageCopy.oneFile,
      name: "notes.md"
    });
    expect(importSheetCanDeclare({
      outcome: "unrecognized",
      refusals: []
    })).toBe(true);
    expect(importSheetCanDeclare({
      outcome: "not_held",
      kind: "mcp_server",
      reason: "the library does not hold MCP servers yet"
    })).toBe(false);
  });

  it("names a mistaken kind in a human sentence, never a token", () => {
    expect(importMistakeSentence("workflow")).toBe(catalogPageCopy.notAWorkflow);
    expect(importMistakeSentence("workflow")).not.toMatch(/invalid-workflow-document|urn:/);
  });

  it("the Import sheet's honourable kinds are workflow and agent, not skill", () => {
    expect(IMPORT_SHEET_KINDS).toEqual(["workflow", "agent"]);
    expect(IMPORT_SHEET_KINDS).not.toContain("skill");
    for (const kind of IMPORT_SHEET_KINDS) {
      expect(importKindLabel(kind)).not.toBe(catalogPageCopy.kindSkill);
    }
  });
});
