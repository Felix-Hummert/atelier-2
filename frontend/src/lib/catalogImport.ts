import type { CatalogIntakeKind, LibraryRecognition } from "../api/client";
import { catalogPageCopy } from "./catalogPageCopy";

/**
 * What the Import sheet shows as recognition's report, and whether that
 * report is a declaration door or a Close-only refusal.
 *
 * The found row is the report. The Kind chips are the decision. Recognition
 * never presses a chip, and a chip is never a found row.
 */
export const CATALOG_INTAKE_KINDS = ["workflow", "agent", "skill"] as const;

export type ImportSheetReport = {
  glyph: string;
  count: string;
  name: string;
};

export function importSheetCanDeclare(recognition: LibraryRecognition | null): boolean {
  return (
    recognition !== null &&
    (recognition.outcome === "workflow" ||
      recognition.outcome === "agent_definition" ||
      recognition.outcome === "unrecognized")
  );
}

export function importSheetReport(
  recognition: LibraryRecognition,
  fileName: string
): ImportSheetReport {
  if (recognition.outcome === "workflow") {
    return {
      glyph: "⧉",
      count: catalogPageCopy.oneWorkflow,
      name: recognition.name ?? catalogPageCopy.unnamedWorkflow
    };
  }
  if (recognition.outcome === "agent_definition") {
    return {
      glyph: "◯",
      count: catalogPageCopy.oneAgent,
      name: recognition.name
    };
  }
  return {
    glyph: "·",
    count: catalogPageCopy.oneFile,
    name: fileName
  };
}

export function importKindLabel(kind: CatalogIntakeKind): string {
  if (kind === "workflow") return catalogPageCopy.kindWorkflow;
  if (kind === "agent") return catalogPageCopy.kindAgent;
  return catalogPageCopy.kindSkill;
}

export function importMistakeSentence(kind: CatalogIntakeKind): string {
  if (kind === "workflow") return catalogPageCopy.notAWorkflow;
  if (kind === "agent") return catalogPageCopy.notAnAgent;
  return catalogPageCopy.notASkill;
}
