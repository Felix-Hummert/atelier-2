import { readFileSync } from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";
import { describe, expect, it } from "vitest";

import { FRONTEND_SRC, svelteImportClosure, svelteSourcesIn } from "../support/workshopSources";

/**
 * REQ-UIQ-10 (loading is a silent skeleton) and REQ-UIQ-13 (one behaviour,
 * one component across surfaces, proven here first): `ReadState`,
 * `AttemptTranscript`, `PinnedDecision`, `V3AnswerCard`, and `HistoryPage`
 * each once drew their own loading state -- a spinner glyph, a dimmed
 * heading, or a plain status line, only `AttemptTranscript`'s conforming to
 * REQ-UIQ-10's skeleton (a shape mark and shape lines, dashed border, no
 * glyph). `HistoryPage` was the fifth, unmeasured occurrence of the same
 * defect (its own `historyPageCopy.looking` literal, found while this issue
 * was already in flight); all five now render
 * `components/LoadingState.svelte`.
 *
 * The proof follows `roomQuestionPattern.test.ts`'s shape: it discovers the
 * shared owner from the skeleton's own CSS signature -- the unique file in
 * the room tree that defines both `.loading-mark` and `.loading-lines` and
 * is imported by another file -- then pins the exact consumer list so a
 * sixth site or a dropped one is caught, not assumed (exact-pin: red on
 * growth and on shrinkage). A second file independently defining that same
 * signature is reported as a second implementation, whether or not it also
 * consumes the owner.
 */

const STYLE_BLOCK = /<style\b[^>]*>([\s\S]*?)<\/style>/i;
const SVELTE_FROM = /\bfrom\s+["']([^"']+\.svelte)["']/g;
const SKELETON_MARK_RULE = /\.loading-mark\b/;
const SKELETON_LINES_RULE = /\.loading-lines\b/;
/** The retired literal every consumer must reach through the copy owner instead of re-declaring. */
const LOOKING_LITERAL = "Looking…";

const EXPECTED_CONSUMERS = [
  "components/AttemptTranscript.svelte",
  "components/PinnedDecision.svelte",
  "components/ReadState.svelte",
  "components/V3AnswerCard.svelte",
  "pages/HistoryPage.svelte"
] as const;

function roomTemplates(): string[] {
  return svelteImportClosure(svelteSourcesIn("pages"));
}

function roomSources(): Map<string, string> {
  return new Map(
    roomTemplates().map((file) => [file, readFileSync(resolve(FRONTEND_SRC, file), "utf8")])
  );
}

function definesSkeleton(source: string): boolean {
  const style = STYLE_BLOCK.exec(source)?.[1] ?? "";
  return SKELETON_MARK_RULE.test(style) && SKELETON_LINES_RULE.test(style);
}

function svelteImports(file: string, source: string): string[] {
  const imported: string[] = [];
  SVELTE_FROM.lastIndex = 0;
  for (const match of source.matchAll(SVELTE_FROM)) {
    const specifier = match[1];
    if (specifier === undefined) continue;
    const resolved = relative(
      FRONTEND_SRC,
      resolve(dirname(resolve(FRONTEND_SRC, file)), specifier)
    )
      .split(sep)
      .join("/");
    if (resolved === "" || resolved.startsWith("..")) continue;
    imported.push(resolved);
  }
  return imported;
}

function sharedSkeletonOwner(files: ReadonlyMap<string, string>): string | null {
  const definers = [...files.entries()]
    .filter(([, source]) => definesSkeleton(source))
    .map(([file]) => file);
  const importedByOthers = new Set<string>();
  for (const [file, source] of files) {
    for (const imported of svelteImports(file, source)) {
      if (imported !== file) importedByOthers.add(imported);
    }
  }
  const shared = definers.filter((file) => importedByOthers.has(file));
  return shared.length === 1 ? (shared[0] ?? null) : null;
}

function secondSkeletonImplementations(
  files: ReadonlyMap<string, string>,
  owner: string
): string[] {
  return [...files.entries()]
    .filter(([file, source]) => file !== owner && definesSkeleton(source))
    .map(([file]) => file)
    .sort();
}

function skeletonConsumers(files: ReadonlyMap<string, string>, owner: string): string[] {
  return [...files.entries()]
    .filter(([file, source]) => file !== owner && svelteImports(file, source).includes(owner))
    .map(([file]) => file)
    .sort();
}

describe("the loading state has one component owner, consumed by name", () => {
  it("discovers the shared skeleton owner as the one file that defines and is imported", () => {
    const sources = new Map([
      [
        "components/LoadingState.svelte",
        "<style>.loading-mark { } .loading-lines { }</style>"
      ],
      [
        "components/ReadState.svelte",
        '<script>import LoadingState from "./LoadingState.svelte";</script>'
      ],
      [
        "pages/Example.svelte",
        '<script>import ReadState from "../components/ReadState.svelte";</script>'
      ]
    ]);
    expect(sharedSkeletonOwner(sources)).toBe("components/LoadingState.svelte");
  });

  it("finds no shared owner when the skeleton is only ever composed inline", () => {
    const sources = new Map([
      [
        "components/AttemptTranscript.svelte",
        "<style>.loading-mark { } .loading-lines { }</style>"
      ],
      ["pages/Example.svelte", "<div>no import</div>"]
    ]);
    expect(sharedSkeletonOwner(sources)).toBeNull();
  });

  it("reports a second file that defines the skeleton signature itself, alongside the owner", () => {
    const owner = "components/LoadingState.svelte";
    const sources = new Map([
      [owner, "<style>.loading-mark { } .loading-lines { }</style>"],
      [
        "components/PinnedDecision.svelte",
        [
          '<script>import LoadingState from "./LoadingState.svelte";</script>',
          "<style>.loading-mark { } .loading-lines { }</style>"
        ].join("\n")
      ]
    ]);
    expect(secondSkeletonImplementations(sources, owner)).toEqual([
      "components/PinnedDecision.svelte"
    ]);
  });

  it("follows each room page's imports into the named loading sites", () => {
    expect(roomTemplates()).toEqual(
      expect.arrayContaining([
        "pages/WorkbenchPage.svelte",
        "pages/CatalogPage.svelte",
        "components/LoadingState.svelte",
        ...EXPECTED_CONSUMERS
      ])
    );
  });

  it("proves(the-loading-state-is-one-component): the room's loading state is the shared skeleton owner, consumed by exactly its named sites", () => {
    const sources = roomSources();
    expect([...sources.keys()]).toEqual(expect.arrayContaining(["pages/WorkbenchPage.svelte"]));
    const owner = sharedSkeletonOwner(sources);
    expect(owner).toBe("components/LoadingState.svelte");
    const duplicates = secondSkeletonImplementations(sources, owner ?? "");
    expect(duplicates, duplicates.join("\n")).toEqual([]);
    const consumers = skeletonConsumers(sources, owner ?? "");
    expect(consumers).toEqual([...EXPECTED_CONSUMERS]);
  });

  it("proves(the-loading-copy-is-not-redeclared): a named consumer reads the label through the copy owner, not a re-declared literal", () => {
    const sources = roomSources();
    for (const file of EXPECTED_CONSUMERS) {
      const source = sources.get(file);
      expect(source, `${file} is part of the room tree`).toBeDefined();
      expect(source?.includes(LOOKING_LITERAL), file).toBe(false);
    }
  });
});
