/**
 * The start sheet's Raw JSON field for an object order (#438 Scheibe 1b): a
 * person may write the whole instance by hand instead of filling the schema
 * form field by field. This module owns the one judgement the sheet needs
 * before it ever proposes those bytes to the server -- whether the text
 * parses as JSON at all -- so a syntax mistake is named with its own
 * position instead of surfacing as an opaque start refusal later. Whether
 * the parsed value satisfies the order's schema is still the server's own
 * judgement (`run-input-refused`); this module never repeats that check.
 */

export type RawOrderJson =
  | { readonly ok: true }
  | { readonly ok: false; readonly reason: string };

/**
 * Whether `text` parses as JSON, and if not, where it broke.
 *
 * `JSON.parse`'s own error is not one shape: this engine sometimes names a
 * line and column already, sometimes only a raw character offset this
 * recovers both from, and sometimes -- a token JSON never expected at all,
 * or empty text -- no position whatsoever. The last case still names the
 * mistake and the way out; it never invents a line or column the engine did
 * not give.
 *
 * The way out itself is the caller's to say, not this module's: an object
 * order that keeps a field form beside Raw JSON has one way out (fill the
 * form instead), while an order Raw JSON alone can reach has another (there
 * is no form to fall back to). `wayOut` carries whichever sentence applies.
 */
export function readRawOrderJson(text: string, wayOut: string): RawOrderJson {
  try {
    JSON.parse(text);
    return { ok: true };
  } catch (error) {
    return { ok: false, reason: `${jsonSyntaxErrorPosition(text, error)} ${wayOut}` };
  }
}

const LINE_AND_COLUMN = /\(line (\d+) column (\d+)\)/;
const CHARACTER_POSITION = /position (\d+)/;

function jsonSyntaxErrorPosition(text: string, error: unknown): string {
  const message = error instanceof Error ? error.message : "invalid JSON";
  const named = LINE_AND_COLUMN.exec(message);
  if (named !== null) {
    return `This is not valid JSON at line ${named[1]}, column ${named[2]}.`;
  }
  const offset = CHARACTER_POSITION.exec(message)?.[1];
  if (offset === undefined) {
    return `This is not valid JSON: ${message}.`;
  }
  const before = text.slice(0, Number(offset));
  const line = before.split("\n").length;
  const column = Number(offset) - before.lastIndexOf("\n");
  return `This is not valid JSON at line ${line}, column ${column}.`;
}
