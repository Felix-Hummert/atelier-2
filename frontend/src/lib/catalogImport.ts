import type { LibraryRecognition } from "../api/client";
import { catalogPageCopy } from "./catalogPageCopy";

/**
 * What the Import sheet shows as recognition's report, and whether that
 * report is a declaration door or a Close-only refusal.
 *
 * The found row is the report. The Kind chips are the decision, and they
 * are only the kinds the catalog can hold as a tile. Recognition never
 * presses a chip, and a chip is never a found row.
 */
export const IMPORT_SHEET_KINDS = ["workflow", "agent"] as const;

export type ImportSheetKind = (typeof IMPORT_SHEET_KINDS)[number];

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

export function importKindLabel(kind: ImportSheetKind): string {
  switch (kind) {
    case "workflow":
      return catalogPageCopy.kindWorkflow;
    case "agent":
      return catalogPageCopy.kindAgent;
  }
}

export function importMistakeSentence(kind: ImportSheetKind): string {
  switch (kind) {
    case "workflow":
      return catalogPageCopy.notAWorkflow;
    case "agent":
      return catalogPageCopy.notAnAgent;
  }
}
