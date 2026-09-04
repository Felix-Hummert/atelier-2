/**
 * What the bytes of a typed wait answer are.
 *
 * A version 3 wait admits whatever its declared JSON schema admits. A schema
 * whose own top level is `type: string` (`WaitAnswerSchemaResourceV3.kind ===
 * "string"`) reads the artifact's raw UTF-8 text as the value itself
 * (`schemas_v3.instance_for_schema` is the one door that decides this) -- so
 * what a person typed travels verbatim, with no JSON-quoting layer around it.
 * Typing `ok` at such a wait used to store `"ok"`, quotes and all: a syntax
 * test rather than a question (operator, 23.08.), reintroduced once the door
 * itself started reading raw text and the composer did not (#1091).
 *
 * Every other schema still asks for a JSON instance, so the composer keeps
 * reading what was typed the way a person means it there too: text that
 * already parses as JSON is passed through exactly as written — that is the
 * expert path, and it needs no toggle because writing JSON *is* the signal —
 * and anything else is sent as the JSON string it plainly is. A schema that
 * refuses the result still refuses it, in the server's own words; this only
 * removes the syntax barrier in front of the question.
 */
export function encodeWaitAnswer(typed: string, isStringSchema: boolean): string {
  const answer = typed.trim();
  if (isStringSchema) return answer;
  return parsesAsJson(answer) ? answer : JSON.stringify(answer);
}

function parsesAsJson(text: string): boolean {
  if (text.length === 0) return false;
  try {
    JSON.parse(text);
    return true;
  } catch {
    return false;
  }
}
