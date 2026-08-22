/**
 * Named axe-core exceptions for the core surfaces.
 *
 * A violation that is not in this list fails CI. A row without an owning
 * GitHub issue is not a baseline entry — it is a swallowed finding.
 *
 * Workflows and History join the Chromium scan (Studio, Project, New Run,
 * the seeded Run page) together under wcag2a / wcag2aa / wcag22aa (66 axe
 * rules) in #526: the Workflows catalog (#527) shipped without ever being
 * added here, and this closes that gap rather than carrying it forward a
 * third time. Widening the scan surfaced one pre-existing Workflows finding
 * outside this change's fence (#534, `axeBaseline.json`); every other
 * surface, including History, reported none.
 */

export const CORE_SURFACES = ["studio", "project", "new-run", "run", "workflows", "history"] as const;

export type CoreSurface = (typeof CORE_SURFACES)[number];

export type AxeBaselineEntry = {
  id: string;
  impact: string;
  helpUrl: string;
  surfaces: readonly CoreSurface[];
  item: string;
};

export type AxeViolation = {
  id: string;
  impact?: string | null;
  helpUrl: string;
};

const ISSUE_URL = /^https:\/\/github\.com\/FlexOr2\/atelier-2\/issues\/\d+$/;

export function baselineEntryProblems(entry: AxeBaselineEntry): string[] {
  const problems: string[] = [];
  if (entry.id.trim() === "") {
    problems.push("id is empty");
  }
  if (entry.impact.trim() === "") {
    problems.push(`${entry.id}: impact is empty`);
  }
  if (entry.helpUrl.trim() === "") {
    problems.push(`${entry.id}: helpUrl is empty`);
  }
  if (entry.surfaces.length === 0) {
    problems.push(`${entry.id}: names no surface`);
  }
  for (const surface of entry.surfaces) {
    if (!CORE_SURFACES.includes(surface)) {
      problems.push(`${entry.id}: unknown surface ${surface}`);
    }
  }
  if (!ISSUE_URL.test(entry.item)) {
    problems.push(`${entry.id}: item is not an atelier-2 issue URL`);
  }
  return problems;
}

export function unnamedAxeViolations(
  surface: CoreSurface,
  violations: readonly AxeViolation[],
  baseline: readonly AxeBaselineEntry[]
): AxeViolation[] {
  return violations.filter((violation) => {
    return !baseline.some(
      (entry) =>
        entry.id === violation.id &&
        entry.helpUrl === violation.helpUrl &&
        entry.surfaces.includes(surface)
    );
  });
}
