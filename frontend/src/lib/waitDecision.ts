/**
 * How a schema-authored decision value reads to a person, one owner for every
 * surface that renders a boolean or enum wait answer (#553, #572): the run
 * page's card and the Board's inline decision both read a click's exact value
 * the same way, so "approve" cannot mean one thing on one surface and
 * `"approve"` (with its quotes) on the other.
 *
 * `stringTyped` names whether `value` is already the raw text a
 * `type: string` schema's door reads verbatim (`WaitAnswerSchemaResourceV3`,
 * #1091 PR #1108 finding 1) -- there it needs no decoding at all; every other
 * enum's `value` is still the JSON-encoded text a decision sends, so it is
 * decoded the way it always was.
 */
export function decisionLabel(value: string, stringTyped: boolean): string {
  if (stringTyped) return value;
  const parsed = JSON.parse(value) as unknown;
  return typeof parsed === "string" ? parsed : JSON.stringify(parsed);
}

/** The confirmed answer's human words, or null where the schema carries no boolean/enum reading. */
export function confirmedDecisionLabel(
  answerKind: "boolean" | "enum" | "string" | "free",
  stringTyped: boolean,
  pendingAnswer: string,
  answerYesLabel: string,
  answerNoLabel: string
): string | null {
  if (answerKind === "boolean") {
    return pendingAnswer === "true" ? answerYesLabel : answerNoLabel;
  }
  if (answerKind === "enum") {
    return decisionLabel(pendingAnswer, stringTyped);
  }
  return null;
}
