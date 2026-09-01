import { readFileSync } from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";
import { describe, expect, it } from "vitest";

import { FRONTEND_SRC, svelteImportClosure, svelteSourcesIn } from "../support/workshopSources";

/**
 * REQ-UIQ-10 (loading is a silent skeleton) and REQ-UIQ-13 (one behaviour,
 * one component across surfaces, proven here first): `ReadState`,
 * `AttemptTranscript`, `PinnedDecision`, `V3AnswerCard`, `NodeDetailPanel`,
 * `V3RunView`, `HistoryPage`, and `RunCockpitPage` each once drew at least
 * one loading rendering of their own -- a spinner glyph, a dimmed heading, a
 * plain status line, or a plain page title -- only `AttemptTranscript`'s
 * conforming to REQ-UIQ-10's skeleton (a shape mark and shape lines, dashed
 * border, no glyph). Four of those (`RunCockpitPage`, `V3RunView` twice,
 * `NodeDetailPanel`, and `V3AnswerCard`'s own second spot) were an
 * independent review's floor, not its ceiling -- re-inventoried by hand
 * against every `*ooking`-named copy reference in the room, not just the
 * lines named. All now render `components/LoadingState.svelte`.
 *
 * The proof follows `roomQuestionPattern.test.ts`'s shape in two parts:
 * 1. it discovers the shared owner from the skeleton's own CSS signature --
 *    the unique file in the room tree that defines both `.loading-mark` and
 *    `.loading-lines` and is imported by another file -- then pins the exact
 *    consumer list so a further site or a dropped one is caught, not
 *    assumed (exact-pin: red on growth and on shrinkage). A second file
 *    independently defining that same signature is reported as a second
 *    implementation, whether or not it also consumes the owner.
 * 2. it runs a second, unpinned check over the *entire* room tree (not the
 *    consumer list above) for any `*ooking`-named copy reference -- the
 *    established naming convention every owner already uses (`looking`,
 *    `questionLooking`, `answerContextLooking`, and any future sibling) --
 *    that renders in a template outside a `<LoadingState>` tag, and for the
 *    retired `"Looking…"` literal reappearing anywhere. A brand new page
 *    that invents its own spinner from an existing or future owned copy
 *    value goes red the moment it is written, before anyone has to
 *    remember to pin it.
 *
 * What this cannot see: a value first built in a `<script>` block (for
 * example a derived page title) and only later rendered as an opaque
 * variable. That class of regression needs ordinary review or a behavioural
 * test at the call site, not a source scan.
 */

const SCRIPT_OR_STYLE = /<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi;
const HTML_COMMENT = /<!--[\s\S]*?-->/g;
const STYLE_BLOCK = /<style\b[^>]*>([\s\S]*?)<\/style>/i;
const SVELTE_FROM = /\bfrom\s+["']([^"']+\.svelte)["']/g;
const SKELETON_MARK_RULE = /\.loading-mark\b/;
const SKELETON_LINES_RULE = /\.loading-lines\b/;
const LOADING_STATE_OPEN_TAG = /<LoadingState\b/g;
/** The established naming convention every copy owner uses for this exact behaviour. */
const LOOKING_FAMILY_REFERENCE = /\.(?:looking|[A-Za-z0-9_]*Looking)\b/g;
/** The retired literal every consumer must reach through the copy owner instead of re-declaring. */
const LOOKING_LITERAL = "Looking…";

const EXPECTED_CONSUMERS = [
  "components/AttemptTranscript.svelte",
  "components/NodeDetailPanel.svelte",
  "components/PinnedDecision.svelte",
  "components/ReadState.svelte",
  "components/V3AnswerCard.svelte",
  "components/V3RunView.svelte",
  "pages/HistoryPage.svelte",
  "pages/RunCockpitPage.svelte"
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

function keepNewlines(text: string): string {
  return text.replace(/[^\n]/g, " ");
}

/** Blanks `<script>`, `<style>`, and comments to characters-preserving-lines, leaving template markup and mustaches. */
function templateOnly(source: string): string {
  return source.replace(SCRIPT_OR_STYLE, keepNewlines).replace(HTML_COMMENT, keepNewlines);
}

function lineAt(source: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index; i += 1) {
    if (source[i] === "\n") line += 1;
  }
  return line;
}

function skipJsQuoted(source: string, start: number): number {
  const quote = source[start];
  if (quote === undefined) return source.length;
  let i = start + 1;
  while (i < source.length) {
    if (source[i] === "\\") {
      i += 2;
      continue;
    }
    if (quote === "`" && source[i] === "$" && source[i + 1] === "{") {
      i = matchingBrace(source, i + 1);
      continue;
    }
    if (source[i] === quote) return i + 1;
    i += 1;
  }
  return source.length;
}

function matchingBrace(source: string, open: number): number {
  let depth = 1;
  let i = open + 1;
  while (i < source.length && depth > 0) {
    const character = source[i];
    if (character === "'" || character === '"' || character === "`") {
      i = skipJsQuoted(source, i);
      continue;
    }
    if (character === "{") depth += 1;
    else if (character === "}") depth -= 1;
    i += 1;
  }
  return i;
}

/** The end of an opening tag's `>`, skipping quoted attribute values and `{...}` expressions so neither hides one. */
function openingTagEnd(source: string, from: number): number {
  let i = from;
  while (i < source.length) {
    const character = source[i];
    if (character === "'" || character === '"' || character === "`") {
      i = skipJsQuoted(source, i);
      continue;
    }
    if (character === "{") {
      i = matchingBrace(source, i);
      continue;
    }
    if (character === ">") return i + 1;
    i += 1;
  }
  return source.length;
}

/** Every `<LoadingState ...>` opening tag's own span -- the one place a `*ooking` reference is allowed to render. */
function loadingStateTagSpans(template: string): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  LOADING_STATE_OPEN_TAG.lastIndex = 0;
  let match = LOADING_STATE_OPEN_TAG.exec(template);
  while (match !== null) {
    const end = openingTagEnd(template, match.index + match[0].length);
    spans.push([match.index, end]);
    LOADING_STATE_OPEN_TAG.lastIndex = end;
    match = LOADING_STATE_OPEN_TAG.exec(template);
  }
  return spans;
}

/**
 * Every `*ooking`-named copy reference rendered in this file's template
 * outside a `<LoadingState>` tag, and every re-declaration of the retired
 * `"Looking…"` literal -- unpinned, so it runs over any file, not only the
 * ones already known to render loading.
 */
function inlineLoadingViolations(file: string, source: string): string[] {
  const template = templateOnly(source);
  const allowed = loadingStateTagSpans(template);
  const found: string[] = [];
  for (const match of template.matchAll(LOOKING_FAMILY_REFERENCE)) {
    const index = match.index ?? 0;
    const withinOwner = allowed.some(([start, end]) => index >= start && index < end);
    if (!withinOwner) found.push(`${file}:${lineAt(template, index)}:${match[0]}`);
  }
  const literalIndex = template.indexOf(LOOKING_LITERAL);
  if (literalIndex !== -1) {
    found.push(`${file}:${lineAt(template, literalIndex)}: re-declares "${LOOKING_LITERAL}"`);
  }
  return found;
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

  it("flags a loading-family copy reference rendered as plain text outside LoadingState", () => {
    expect(
      inlineLoadingViolations("pages/Example.svelte", "<p>{runPageCopy.questionLooking}</p>")
    ).toEqual(["pages/Example.svelte:1:.questionLooking"]);
  });

  it("does not flag the same reference passed as LoadingState's own label prop, single- or multi-line", () => {
    expect(
      inlineLoadingViolations(
        "pages/Example.svelte",
        '<LoadingState label={runPageCopy.questionLooking} compact />'
      )
    ).toEqual([]);
    expect(
      inlineLoadingViolations(
        "pages/Example.svelte",
        ['<LoadingState', '  label={x ? readStateCopy.looking : readStateCopy.refreshing}', '  compact', '/>'].join(
          "\n"
        )
      )
    ).toEqual([]);
  });

  it("flags a re-declared literal even in a file with no pinned consumer status", () => {
    expect(
      inlineLoadingViolations("pages/Example.svelte", '<p role="status">Looking…</p>')
    ).toEqual(['pages/Example.svelte:1: re-declares "Looking…"']);
  });

  it("ignores a script-only definition of the retired literal, the copy owner's own case", () => {
    expect(
      inlineLoadingViolations(
        "lib/readStateCopy.svelte",
        '<script>export const looking = "Looking…";</script>\n<p></p>'
      )
    ).toEqual([]);
  });

  it("proves(no-loading-state-renders-outside-the-owner): every loading-family reference in the whole room tree renders through LoadingState, and the retired literal never reappears", () => {
    const sources = roomSources();
    const violations = [...sources.entries()].flatMap(([file, source]) =>
      inlineLoadingViolations(file, source)
    );
    expect(violations, violations.join("\n")).toEqual([]);
  });
});
