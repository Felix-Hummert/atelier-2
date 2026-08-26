import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  baselineEntryProblems,
  unnamedAxeViolations,
  type AxeBaselineEntry
} from "../support/axeBaseline";

const checkedIn = JSON.parse(
  readFileSync(resolve(process.cwd(), "tests/support/axeBaseline.json"), "utf8")
) as AxeBaselineEntry[];

describe("the axe baseline is a named exception list", () => {
  it("does not occupy the production source graph", () => {
    expect(existsSync(resolve(process.cwd(), "src/lib/axeBaseline.ts"))).toBe(false);
    expect(existsSync(resolve(process.cwd(), "src/lib/axeBaseline.json"))).toBe(false);
  });

  it("every checked-in row names an issue and a core surface", () => {
    expect(Array.isArray(checkedIn)).toBe(true);
    for (const entry of checkedIn) {
      expect(baselineEntryProblems(entry), JSON.stringify(entry)).toEqual([]);
    }
  });

  it("a violation the baseline does not name stays unnamed", () => {
    const baseline: AxeBaselineEntry[] = [
      {
        id: "color-contrast",
        impact: "serious",
        helpUrl: "https://dequeuniversity.com/rules/axe/4.13/color-contrast",
        surfaces: ["workbench"],
        item: "https://github.com/FlexOr2/atelier-2/issues/336"
      }
    ];

    expect(
      unnamedAxeViolations(
        "workbench",
        [
          {
            id: "button-name",
            impact: "critical",
            helpUrl: "https://dequeuniversity.com/rules/axe/4.13/button-name"
          }
        ],
        baseline
      )
    ).toHaveLength(1);

    expect(
      unnamedAxeViolations(
        "workbench",
        [
          {
            id: "color-contrast",
            impact: "serious",
            helpUrl: "https://dequeuniversity.com/rules/axe/4.13/color-contrast"
          }
        ],
        baseline
      )
    ).toEqual([]);

    expect(
      unnamedAxeViolations(
        "run",
        [
          {
            id: "color-contrast",
            impact: "serious",
            helpUrl: "https://dequeuniversity.com/rules/axe/4.13/color-contrast"
          }
        ],
        baseline
      )
    ).toHaveLength(1);
  });

  it("a row without an issue is not a baseline entry", () => {
    expect(
      baselineEntryProblems({
        id: "color-contrast",
        impact: "serious",
        helpUrl: "https://dequeuniversity.com/rules/axe/4.13/color-contrast",
        surfaces: ["workbench"],
        item: "later"
      })
    ).toContain("color-contrast: item is not an atelier-2 issue URL");
  });
});
