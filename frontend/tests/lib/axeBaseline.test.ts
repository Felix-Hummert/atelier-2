import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  baselineEntryProblems,
  unnamedAxeViolations,
  type AxeBaselineEntry
} from "../../src/lib/axeBaseline";

const checkedIn = JSON.parse(
  readFileSync(resolve(process.cwd(), "src/lib/axeBaseline.json"), "utf8")
) as AxeBaselineEntry[];

describe("the axe baseline is a named exception list", () => {
  it("proves(core-surfaces-have-no-unnamed-axe-violations): every checked-in row names an issue and a core surface", () => {
    expect(Array.isArray(checkedIn)).toBe(true);
    for (const entry of checkedIn) {
      expect(baselineEntryProblems(entry), JSON.stringify(entry)).toEqual([]);
    }
  });

  it("proves(core-surfaces-have-no-unnamed-axe-violations): a violation the baseline does not name stays unnamed", () => {
    const baseline: AxeBaselineEntry[] = [
      {
        id: "color-contrast",
        impact: "serious",
        helpUrl: "https://dequeuniversity.com/rules/axe/4.13/color-contrast",
        surfaces: ["studio"],
        item: "https://github.com/FlexOr2/atelier-2/issues/336"
      }
    ];

    expect(
      unnamedAxeViolations(
        "studio",
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
        "studio",
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

  it("proves(core-surfaces-have-no-unnamed-axe-violations): a row without an issue is not a baseline entry", () => {
    expect(
      baselineEntryProblems({
        id: "color-contrast",
        impact: "serious",
        helpUrl: "https://dequeuniversity.com/rules/axe/4.13/color-contrast",
        surfaces: ["studio"],
        item: "later"
      })
    ).toContain("color-contrast: item is not an atelier-2 issue URL");
  });
});
