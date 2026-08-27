import { readdirSync } from "node:fs";
import { resolve } from "node:path";

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
