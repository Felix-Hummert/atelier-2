/** Age and exact local stamps. The store is UTC; the surface is local. */

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function parseUtc(value: string): Date {
  return new Date(value.endsWith("Z") ? value : `${value}Z`);
}

export function ageLabel(
  iso: string,
  now: Date,
  kind: "ago" | "for" | "duration",
  endedIso?: string
): string {
  const start = parseUtc(iso).getTime();
  const end = endedIso === undefined ? now.getTime() : parseUtc(endedIso).getTime();
  const elapsed = Math.max(0, end - start);
  const words = spanWords(elapsed);
  if (kind === "for") return `for ${words}`;
  if (kind === "duration") return words;
  return elapsed < MINUTE ? "just now" : `${words} ago`;
}

export function exactLocal(iso: string): string {
  return parseUtc(iso).toLocaleString();
}

function spanWords(elapsed: number): string {
  if (elapsed < MINUTE) return `${Math.floor(elapsed / 1000)} s`;
  if (elapsed < HOUR) return `${Math.floor(elapsed / MINUTE)} min`;
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)} h`;
  return `${Math.floor(elapsed / DAY)} d`;
}
