/**
 * What the bytes of a typed wait answer are.
 *
 * A version 3 wait admits whatever its declared JSON schema admits, so the
 * bytes on the wire are JSON. A person answering "ok" is not writing JSON and
 * should not have to: typing `ok` used to be refused until it was typed as
 * `"ok"`, which is a syntax test rather than a question (operator, 23.08.).
 *
 * So the composer reads what was typed the way a person means it: text that
 * already parses as JSON is passed through exactly as written — that is the
 * expert path, and it needs no toggle because writing JSON *is* the signal —
 * and anything else is sent as the JSON string it plainly is. A schema that
 * refuses the result still refuses it, in the server's own words; this only
 * removes the syntax barrier in front of the question.
 */
export function encodeWaitAnswer(typed: string): string {
  const answer = typed.trim();
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
