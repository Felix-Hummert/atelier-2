/**
 * How a schema-authored JSON decision value reads to a person, one owner for
 * every surface that renders a boolean or enum wait answer (#553, #572): the
 * run page's card and the Board's inline decision both read a click's exact
 * JSON value the same way, so "approve" cannot mean one thing on one surface
 * and `"approve"` (with its quotes) on the other.
 */
export function decisionLabel(jsonEncoded: string): string {
  const parsed = JSON.parse(jsonEncoded) as unknown;
  return typeof parsed === "string" ? parsed : JSON.stringify(parsed);
}

/** The confirmed answer's human words, or null where the schema carries no boolean/enum reading. */
export function confirmedDecisionLabel(
  answerKind: "boolean" | "enum" | "string" | "free",
  pendingAnswer: string,
  answerYesLabel: string,
  answerNoLabel: string
): string | null {
  if (answerKind === "boolean") {
    return pendingAnswer === "true" ? answerYesLabel : answerNoLabel;
  }
  if (answerKind === "enum") {
    return decisionLabel(pendingAnswer);
  }
  return null;
}
