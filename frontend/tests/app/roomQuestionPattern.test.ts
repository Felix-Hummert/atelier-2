import { dirname, relative, resolve, sep } from "node:path";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  FRONTEND_SRC,
  svelteImportClosure,
  svelteSourcesIn
} from "../support/workshopSources";

/**
 * REQ-UIQ-07: a question has a pattern, and the pattern is a reused
 * component. A question in this check is a `<dialog>`, an element with
 * `role="dialog"` or `role="alertdialog"`, or a script-level `confirm` /
 * `prompt` (`window`, `globalThis`, or the global) inside a room page or
 * a `.svelte` file that page imports, transitively.
 *
 * The shared question component is the unique file that both composes a
 * question and is imported by another file in the room tree. A question
 * is allowed in that file; everywhere else it must be a rendering of
 * that file, not markup the host composed. Class names may help a
 * reader find a candidate; they never identify a question and they
 * never excuse one. A file is not exempt because its first element
 * looks like a question surface.
 *
 * Today's tree has no such shared module: nine imported files each
 * compose their own `<dialog>`. The sentence names them and does not
 * prove they share one Stage or Sheet. A `createElement` overlay, a
 * question in a `.ts` module, a dynamic `role={...}`, a sheet that is
 * not a dialog, and an in-page decision stage that is not a dialog or
 * confirm (PinnedDecision, V3AnswerCard, HumanActionCard, the Workbench
 * ear) are outside what the templates and script tags show.
 */

const SCRIPT_OR_STYLE = /<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi;
const SCRIPT_OPEN = /<script\b[^>]*>/gi;
const SCRIPT_CLOSE = "</script>";
const HTML_COMMENT = /<!--[\s\S]*?-->/g;
const ATTRIBUTE_NAME = /[A-Za-z_:][\w:.-]*/y;
const SVELTE_FROM = /\bfrom\s+["']([^"']+\.svelte)["']/g;
const DIALOG_ROLES = new Set(["dialog", "alertdialog"]);
const GLOBAL_ASKS = new Set(["confirm", "prompt"]);

const NAMED_RESIDUAL_FILES = [
  "components/AddModelSheet.svelte",
  "components/CatalogImportSheet.svelte",
  "components/ConnectSourceSheet.svelte",
  "components/DisconnectSourceSheet.svelte",
  "components/ReconciliationActionCard.svelte",
  "components/RenewSourceTokenSheet.svelte",
  "components/RunCancelCard.svelte",
  "components/RunForkSheet.svelte",
  "components/WorkflowStartSheet.svelte"
] as const;

interface StartTag {
  name: string;
  role: string | null;
  index: number;
  text: string;
}

function roomTemplates(): string[] {
  return svelteImportClosure(svelteSourcesIn("pages"));
}

function blankNonTemplate(source: string): string {
  return source.replace(SCRIPT_OR_STYLE, keepNewlines).replace(HTML_COMMENT, keepNewlines);
}

function keepNewlines(text: string): string {
  return text.replace(/[^\n]/g, " ");
}

function lineAt(source: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index; i += 1) {
    if (source[i] === "\n") line += 1;
  }
  return line;
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
    if (character === "/" && source[i + 1] === "/") {
      const newline = source.indexOf("\n", i);
      i = newline === -1 ? source.length : newline;
      continue;
    }
    if (character === "/" && source[i + 1] === "*") {
      const close = source.indexOf("*/", i + 2);
      i = close === -1 ? source.length : close + 2;
      continue;
    }
    if (character === "{") depth += 1;
    else if (character === "}") depth -= 1;
    i += 1;
  }
  return i;
}

function skipJsQuoted(source: string, start: number): number {
  const quote = source[start];
  if (quote === undefined) return source.length;
  let i = start + 1;
  if (quote === "`") {
    while (i < source.length) {
      if (source[i] === "\\") {
        i += 2;
        continue;
      }
      if (source[i] === "`") return i + 1;
      if (source[i] === "$" && source[i + 1] === "{") {
        i = matchingBrace(source, i + 1);
        continue;
      }
      i += 1;
    }
    return source.length;
  }
  while (i < source.length) {
    if (source[i] === "\\") {
      i += 2;
      continue;
    }
    if (source[i] === quote) return i + 1;
    i += 1;
  }
  return source.length;
}

function skipHtmlQuoted(source: string, start: number): number {
  const quote = source[start];
  if (quote === undefined) return source.length;
  const close = source.indexOf(quote, start + 1);
  return close === -1 ? source.length : close + 1;
}

function isHtmlTag(name: string): boolean {
  const first = name[0];
  return first !== undefined && first === first.toLowerCase();
}

function report(file: string, source: string, index: number, text: string): string {
  return `${file}:${lineAt(source, index)}:${text}`;
}

function readAttributes(
  source: string,
  start: number
): { role: string | null; end: number } {
  let role: string | null = null;
  let i = start;
  while (i < source.length) {
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (i >= source.length) break;
    if (source[i] === ">") return { role, end: i + 1 };
    if (source[i] === "/" && source[i + 1] === ">") return { role, end: i + 2 };
    if (source[i] === "{") {
      i = matchingBrace(source, i);
      continue;
    }
    ATTRIBUTE_NAME.lastIndex = i;
    const name = ATTRIBUTE_NAME.exec(source);
    if (name === null) {
      i += 1;
      continue;
    }
    const attribute = name[0];
    i = ATTRIBUTE_NAME.lastIndex;
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (source[i] !== "=") continue;
    i += 1;
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (source[i] === "'" || source[i] === '"') {
      const close = skipHtmlQuoted(source, i);
      if (attribute === "role") role = source.slice(i + 1, close - 1);
      i = close;
      continue;
    }
    if (source[i] === "{") {
      i = matchingBrace(source, i);
    }
  }
  return { role, end: source.length };
}

function nextStartTag(source: string, from: number): { tag: StartTag; end: number } | null {
  let i = from;
  while (i < source.length) {
    const character = source[i];
    if (character === "{") {
      i = matchingBrace(source, i);
      continue;
    }
    if (character !== "<") {
      i += 1;
      continue;
    }
    if (source[i + 1] === "/") {
      const close = source.indexOf(">", i + 2);
      i = close === -1 ? source.length : close + 1;
      continue;
    }
    if (source[i + 1] === "!" || source[i + 1] === "?") {
      const close = source.indexOf(">", i + 2);
      i = close === -1 ? source.length : close + 1;
      continue;
    }
    let nameEnd = i + 1;
    while (nameEnd < source.length && /[A-Za-z0-9:-]/.test(source[nameEnd] ?? "")) nameEnd += 1;
    const name = source.slice(i + 1, nameEnd);
    if (name.length === 0) {
      i += 1;
      continue;
    }
    const attributes = readAttributes(source, nameEnd);
    const text = source.slice(i, attributes.end).replace(/\s+/g, " ").trim();
    return {
      tag: { name, role: attributes.role, index: i, text },
      end: attributes.end
    };
  }
  return null;
}

function startTags(source: string): StartTag[] {
  const found: StartTag[] = [];
  let i = 0;
  while (i < source.length) {
    const next = nextStartTag(source, i);
    if (next === null) break;
    found.push(next.tag);
    i = next.end;
  }
  return found;
}

function isComposedQuestionTag(tag: StartTag): boolean {
  if (!isHtmlTag(tag.name)) return false;
  if (tag.name === "dialog") return true;
  return tag.role !== null && DIALOG_ROLES.has(tag.role);
}

function scriptBodies(source: string): Array<{ start: number; body: string }> {
  const bodies: Array<{ start: number; body: string }> = [];
  SCRIPT_OPEN.lastIndex = 0;
  let match = SCRIPT_OPEN.exec(source);
  while (match !== null) {
    const start = match.index + match[0].length;
    const close = source.toLowerCase().indexOf(SCRIPT_CLOSE, start);
    if (close === -1) {
      bodies.push({ start, body: source.slice(start) });
      break;
    }
    bodies.push({ start, body: source.slice(start, close) });
    SCRIPT_OPEN.lastIndex = close + SCRIPT_CLOSE.length;
    match = SCRIPT_OPEN.exec(source);
  }
  return bodies;
}

function skipSpace(source: string, index: number): number {
  let i = index;
  while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
  return i;
}

function identifierAt(source: string, index: number): { name: string; end: number } | null {
  if (index >= source.length || !/[A-Za-z_$]/.test(source[index] ?? "")) return null;
  let end = index + 1;
  while (end < source.length && /[\w$]/.test(source[end] ?? "")) end += 1;
  return { name: source.slice(index, end), end };
}

function boundAskNames(script: string): Set<string> {
  const bound = new Set<string>();
  let i = 0;
  while (i < script.length) {
    const character = script[i];
    if (character === "'" || character === '"' || character === "`") {
      i = skipJsQuoted(script, i);
      continue;
    }
    if (character === "/" && script[i + 1] === "/") {
      const newline = script.indexOf("\n", i);
      i = newline === -1 ? script.length : newline;
      continue;
    }
    if (character === "/" && script[i + 1] === "*") {
      const close = script.indexOf("*/", i + 2);
      i = close === -1 ? script.length : close + 2;
      continue;
    }
    const ident = identifierAt(script, i);
    if (ident === null) {
      i += 1;
      continue;
    }
    if (
      ident.name === "function" ||
      ident.name === "const" ||
      ident.name === "let" ||
      ident.name === "var"
    ) {
      const next = identifierAt(script, skipSpace(script, ident.end));
      if (next !== null && GLOBAL_ASKS.has(next.name)) bound.add(next.name);
      i = ident.end;
      continue;
    }
    if (ident.name === "import") {
      bindImportedAsks(script, ident.end, bound);
      i = ident.end;
      continue;
    }
    i = ident.end;
  }
  return bound;
}

function bindImportedAsks(script: string, afterImport: number, bound: Set<string>): void {
  const i = skipSpace(script, afterImport);
  if (script[i] === "{") {
    const close = script.indexOf("}", i + 1);
    const specifiers = close === -1 ? script.slice(i + 1) : script.slice(i + 1, close);
    for (const specifier of specifiers.split(",")) {
      const names = specifier.trim().split(/\s+as\s+/);
      const local = names[names.length - 1]?.trim();
      if (local !== undefined && GLOBAL_ASKS.has(local)) bound.add(local);
    }
    return;
  }
  const ident = identifierAt(script, i);
  if (ident !== null && GLOBAL_ASKS.has(ident.name)) bound.add(ident.name);
}

function skipSpaceBack(source: string, index: number): number {
  let i = index;
  while (i >= 0 && /\s/.test(source[i] ?? "")) i -= 1;
  return i;
}

function memberRoot(source: string, nameStart: number): string | null {
  let i = skipSpaceBack(source, nameStart - 1);
  if (source[i] !== ".") return null;
  i = skipSpaceBack(source, i - 1);
  if (i < 0 || !/[\w$]/.test(source[i] ?? "")) return null;
  let start = i;
  while (start > 0 && /[\w$]/.test(source[start - 1] ?? "")) start -= 1;
  return source.slice(start, i + 1);
}

function isGlobalAskCall(
  script: string,
  nameStart: number,
  name: string,
  bound: ReadonlySet<string>
): boolean {
  if (!GLOBAL_ASKS.has(name)) return false;
  const root = memberRoot(script, nameStart);
  if (root !== null) return root === "window" || root === "globalThis";
  return !bound.has(name);
}

function callOrigin(script: string, nameStart: number): number {
  if (memberRoot(script, nameStart) === null) return nameStart;
  let i = skipSpaceBack(script, nameStart - 1);
  i = skipSpaceBack(script, i - 1);
  let start = i;
  while (start > 0 && /[\w$]/.test(script[start - 1] ?? "")) start -= 1;
  return start;
}

function callText(script: string, nameStart: number, open: number): string {
  let i = open + 1;
  let depth = 1;
  while (i < script.length && depth > 0) {
    const character = script[i];
    if (character === "'" || character === '"' || character === "`") {
      i = skipJsQuoted(script, i);
      continue;
    }
    if (character === "(") depth += 1;
    else if (character === ")") depth -= 1;
    i += 1;
  }
  return script.slice(callOrigin(script, nameStart), i).replace(/\s+/g, " ").trim();
}

function scanScriptAsks(
  file: string,
  source: string,
  bodyStart: number,
  body: string
): string[] {
  const bound = boundAskNames(body);
  const found: string[] = [];
  let i = 0;
  while (i < body.length) {
    const character = body[i];
    if (character === "'" || character === '"' || character === "`") {
      i = skipJsQuoted(body, i);
      continue;
    }
    if (character === "/" && body[i + 1] === "/") {
      const newline = body.indexOf("\n", i);
      i = newline === -1 ? body.length : newline;
      continue;
    }
    if (character === "/" && body[i + 1] === "*") {
      const close = body.indexOf("*/", i + 2);
      i = close === -1 ? body.length : close + 2;
      continue;
    }
    const ident = identifierAt(body, i);
    if (ident === null) {
      i += 1;
      continue;
    }
    const open = skipSpace(body, ident.end);
    if (body[open] === "(" && isGlobalAskCall(body, i, ident.name, bound)) {
      found.push(report(file, source, bodyStart + i, callText(body, i, open)));
    }
    i = ident.end;
  }
  return found;
}

function templateQuestions(file: string, source: string): string[] {
  const template = blankNonTemplate(source);
  return startTags(template)
    .filter(isComposedQuestionTag)
    .map((tag) => report(file, source, tag.index, tag.text));
}

function scriptQuestions(file: string, source: string): string[] {
  return scriptBodies(source).flatMap(({ start, body }) =>
    scanScriptAsks(file, source, start, body)
  );
}

function questionsIn(file: string, source: string): string[] {
  return [...templateQuestions(file, source), ...scriptQuestions(file, source)];
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

function sharedQuestionComponent(files: ReadonlyMap<string, string>): string | null {
  const composers = [...files.entries()]
    .filter(([file, source]) => questionsIn(file, source).length > 0)
    .map(([file]) => file);
  const importedByOthers = new Set<string>();
  for (const [file, source] of files) {
    for (const imported of svelteImports(file, source)) {
      if (imported !== file) importedByOthers.add(imported);
    }
  }
  const shared = composers.filter((file) => importedByOthers.has(file));
  return shared.length === 1 ? (shared[0] ?? null) : null;
}

function composedQuestionsIn(
  file: string,
  source: string,
  shared: string | null = null
): string[] {
  if (file === shared) return [];
  return questionsIn(file, source);
}

function roomSources(): Map<string, string> {
  return new Map(
    roomTemplates().map((file) => [file, readFileSync(resolve(FRONTEND_SRC, file), "utf8")])
  );
}

function fileOf(reportLine: string): string {
  const lineStart = reportLine.indexOf(":");
  return lineStart === -1 ? reportLine : reportLine.slice(0, lineStart);
}

describe("a question is the shared component, not markup a host composed", () => {
  it("reports the file, line, and opening tag for a dialog nested in a room page", () => {
    expect(
      composedQuestionsIn(
        "pages/Example.svelte",
        '<section class="surface"><dialog class="sheet" aria-label="Import"></dialog></section>'
      )
    ).toEqual(['pages/Example.svelte:1:<dialog class="sheet" aria-label="Import">']);
  });

  it("reports a role=dialog overlay composed beside another surface", () => {
    expect(
      composedQuestionsIn(
        "components/Host.svelte",
        '<section class="v3-run"><div role="dialog" class="sheet">Stop this run?</div></section>'
      )
    ).toEqual(['components/Host.svelte:1:<div role="dialog" class="sheet">']);
  });

  it("does not treat a class name as a question, and does not excuse a dialog because one is present", () => {
    expect(
      composedQuestionsIn(
        "pages/Example.svelte",
        '<section class="workbench"><section class="pinned-decision"><h2>Ship it?</h2></section></section>'
      )
    ).toEqual([]);
    expect(
      composedQuestionsIn(
        "components/Host.svelte",
        '<section class="cancel decision human-action pinned-decision sheet-positioner"><dialog class="dialog"></dialog></section>'
      )
    ).toEqual(['components/Host.svelte:1:<dialog class="dialog">']);
  });

  it("treats a rendering of an imported question component as reuse, not as composed markup", () => {
    expect(
      composedQuestionsIn(
        "pages/Example.svelte",
        [
          '<section class="surface">',
          "  {#if open}<AddModelSheet onClose={close} />{/if}",
          "  <PinnedDecision {run} />",
          "  <CatalogImportSheet {document} />",
          "</section>"
        ].join("\n")
      )
    ).toEqual([]);
  });

  it("reports a sheet that composes its own dialog instead of rendering the shared component", () => {
    expect(
      composedQuestionsIn(
        "components/AddModelSheet.svelte",
        '<div class="sheet-positioner"><dialog class="sheet" aria-labelledby="title"></dialog></div>'
      )
    ).toEqual([
      'components/AddModelSheet.svelte:1:<dialog class="sheet" aria-labelledby="title">'
    ]);
  });

  it("reports a root dialog that is not the shared question component", () => {
    expect(
      composedQuestionsIn(
        "components/RunForkSheet.svelte",
        '<dialog class="dialog" aria-label="Start again from this node"></dialog>'
      )
    ).toEqual([
      'components/RunForkSheet.svelte:1:<dialog class="dialog" aria-label="Start again from this node">'
    ]);
  });

  it("reports a confirmation dialog nested in another surface of the same file", () => {
    expect(
      composedQuestionsIn(
        "components/RunCancelCard.svelte",
        [
          '<section class="cancel">',
          '  <button type="button">Stop this run</button>',
          "</section>",
          '{#if confirming}<dialog class="dialog" aria-labelledby="question"></dialog>{/if}'
        ].join("\n")
      )
    ).toEqual([
      'components/RunCancelCard.svelte:4:<dialog class="dialog" aria-labelledby="question">'
    ]);
  });

  it("leaves the shared question component's own dialog unreported", () => {
    expect(
      composedQuestionsIn(
        "components/Sheet.svelte",
        '<dialog class="sheet" aria-labelledby="title"></dialog>',
        "components/Sheet.svelte"
      )
    ).toEqual([]);
  });

  it("discovers the shared component as the one imported composer", () => {
    const sources = new Map([
      [
        "components/Sheet.svelte",
        '<dialog class="sheet" aria-labelledby="title"></dialog>'
      ],
      [
        "components/AddModelSheet.svelte",
        '<script>import Sheet from "./Sheet.svelte";</script>\n<Sheet />'
      ],
      [
        "pages/SettingsPage.svelte",
        '<script>import AddModelSheet from "../components/AddModelSheet.svelte";</script>\n<AddModelSheet />'
      ]
    ]);
    expect(sharedQuestionComponent(sources)).toBe("components/Sheet.svelte");
  });

  it("reports a script-level confirm in a page, and ignores a local function of the same name", () => {
    expect(
      composedQuestionsIn(
        "pages/Example.svelte",
        ["<script>", '  window.confirm("Stop this run?");', "</script>", "<section></section>"].join(
          "\n"
        )
      )
    ).toEqual(['pages/Example.svelte:2:window.confirm("Stop this run?")']);
    expect(
      composedQuestionsIn(
        "pages/Example.svelte",
        [
          "<script>",
          "  function confirm(generation, confirmed) { live = confirmed; }",
          "  confirm(1, snapshot);",
          "</script>",
          "<section></section>"
        ].join("\n")
      )
    ).toEqual([]);
  });

  it("follows each room page's .svelte imports into the question components those rooms host", () => {
    expect(roomTemplates()).toEqual(
      expect.arrayContaining([
        "pages/WorkbenchPage.svelte",
        "pages/CatalogPage.svelte",
        "pages/HistoryPage.svelte",
        "pages/RunCockpitPage.svelte",
        "pages/SettingsPage.svelte",
        "pages/WorkflowDetailPage.svelte",
        "components/AddModelSheet.svelte",
        "components/CatalogImportSheet.svelte",
        "components/ConnectSourceSheet.svelte",
        "components/DisconnectSourceSheet.svelte",
        "components/RenewSourceTokenSheet.svelte",
        "components/WorkflowStartSheet.svelte",
        "components/RunForkSheet.svelte",
        "components/V3AnswerCard.svelte",
        "components/PinnedDecision.svelte",
        "components/HumanActionCard.svelte",
        "components/ReconciliationActionCard.svelte",
        "components/RunCancelCard.svelte",
        "components/V3RunView.svelte"
      ])
    );
  });

  it("proves(a-question-uses-the-shared-question-pattern): a question in a room is the shared question component or a rendering of it", () => {
    const sources = roomSources();
    expect([...sources.keys()]).toEqual(expect.arrayContaining(["pages/WorkbenchPage.svelte"]));
    const shared = sharedQuestionComponent(sources);
    expect(shared).toBeNull();
    const violations = [...sources.entries()].flatMap(([file, source]) =>
      composedQuestionsIn(file, source, shared)
    );
    expect([...new Set(violations.map(fileOf))].sort(), violations.join("\n")).toEqual([
      ...NAMED_RESIDUAL_FILES
    ]);
  });
});
