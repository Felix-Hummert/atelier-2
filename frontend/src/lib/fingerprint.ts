/**
 * How a long machine value is written where a person can see it.
 *
 * A 64-character digest read in full is noise, and a digest hidden behind a
 * reveal is a label with no value — a riddle chip (operator, 23.08.). So the
 * value is always on screen, shortened at both ends: the head and the tail are
 * what a person compares against a receipt, and the exact bytes stay one copy
 * away.
 *
 * Only a digest is shortened. A speaking identifier — `v3/seen-in-the-browser`
 * — is content a person reads, not a machine value to compare, so it is left
 * exactly as written however long it is (operator ruling 23.08.).
 */
const HEAD = 8;
const TAIL = 4;
const DIGEST = /^[0-9a-f]{32,}$/i;

export function shortFingerprint(value: string): string {
  if (!DIGEST.test(value)) return value;
  return `${value.slice(0, HEAD)}…${value.slice(-TAIL)}`;
}
