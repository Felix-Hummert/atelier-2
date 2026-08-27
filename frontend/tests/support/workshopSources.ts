import { readdirSync, readFileSync } from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";

/** The cockpit source root the inventory tests read. */
export const FRONTEND_SRC = resolve(import.meta.dirname, "../../src");

/** Relative `.svelte` paths under each named directory of `FRONTEND_SRC`. */
export function svelteSourcesIn(...directories: string[]): string[] {
  return directories.flatMap((directory) =>
    readdirSync(resolve(FRONTEND_SRC, directory))
      .filter((name) => name.endsWith(".svelte"))
      .map((name) => `${directory}/${name}`)
  );
}

const SVELTE_FROM = /\bfrom\s+["']([^"']+\.svelte)["']/g;

/**
 * Relative `.svelte` paths reachable from the given entries by following
 * `from ".../*.svelte"` imports. `.ts` imports are not followed. Cycles
 * are skipped; the result is unique and sorted.
 */
export function svelteImportClosure(entries: readonly string[]): string[] {
  const seen = new Set<string>();
  const pending = [...entries];
  while (pending.length > 0) {
    const file = pending.pop();
    if (file === undefined || seen.has(file)) continue;
    seen.add(file);
    const source = readFileSync(resolve(FRONTEND_SRC, file), "utf8");
    SVELTE_FROM.lastIndex = 0;
    for (const match of source.matchAll(SVELTE_FROM)) {
      const specifier = match[1];
      if (specifier === undefined) continue;
      const imported = toFrontendRelative(resolve(dirname(resolve(FRONTEND_SRC, file)), specifier));
      if (imported !== null) pending.push(imported);
    }
  }
  return [...seen].sort();
}

function toFrontendRelative(absolute: string): string | null {
  const imported = relative(FRONTEND_SRC, absolute);
  if (imported === "" || imported.startsWith(`..${sep}`) || imported === "..") return null;
  return imported.split(sep).join("/");
}
