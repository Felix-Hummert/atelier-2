import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  FRONTEND_SRC,
  svelteImportClosure,
  svelteSourcesIn
} from "../support/workshopSources";

/**
 * REQ-UIQ-03: terms of a surface come from one source. Room pages render
 * through `wrapDisplayCopy` and the `*Copy` / shared display-copy owners;
 * a hand-written operator-facing literal in those templates, or a
 * literal passed to a display sink, is a second source. Structural
 * glyphs, protocol tokens, and whitespace are not terms.
 *
 * A script literal counts as display copy only when it reaches a known
 * display sink (a function in `DISPLAY_SINKS`, or a display field such
 * as `label` / `title` / `message`). A literal reaching an unlisted
 * callee is not reported. A thrown message is display copy only when
 * that throw itself reaches a display sink — not because the file also
 * renders `error.message`.
 *
 * The file list is every room page and every `.svelte` file those pages
 * import, transitively.
 */

const VISIBLE_ATTRIBUTE_KEYS = new Set([
  "alt",
  "ariadescription",
  "arialabel",
  "ariaplaceholder",
  "ariaroledescription",
  "ariavaluetext",
  "datalabel",
  "label",
  "placeholder",
  "seals",
  "title"
]);

const SCRIPT_OR_STYLE = /<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi;
const SCRIPT_OPEN = /<script\b[^>]*>/gi;
const SCRIPT_CLOSE = "</script>";
const HTML_COMMENT = /<!--[\s\S]*?-->/g;
const ATTRIBUTE_NAME = /[A-Za-z_:][\w:.-]*/y;
const COMPARAND_PREFIX = /(?:===|!==|==|!=|case)$/;
const PATH_OR_URL = /^(?:\.{0,2}\/|[a-z][a-z0-9+.-]*:)/i;
/**
 * Functions that receive operator-facing copy as an argument in today's
 * room tree. A script literal counts as display copy only when it
 * reaches one of these; a literal reaching an unlisted callee is not
 * reported.
 */
const DISPLAY_SINKS = new Set([
  "confirmedDecisionLabel",
  "copyLabel",
  "deliverAndSettle",
  "deliverCancel",
  "deliverWaitAnswer",
  "encodedContext",
  "humanErrorMessage",
  "proofSealsSentence",
  "retryLabel",
  "wrapDisplayCopy"
]);
const DISPLAY_FIELDS = new Set([
  "alt",
  "ariadescription",
  "arialabel",
  "ariaplaceholder",
  "ariaroledescription",
  "ariavaluetext",
  "datalabel",
  "heading",
  "label",
  "message",
  "placeholder",
  "prose",
  "seals",
  "text",
  "title"
]);

function roomTemplates(): string[] {
  return svelteImportClosure(svelteSourcesIn("pages"));
}

function unownedCopyIn(file: string, source: string): string[] {
  return [...scanTemplate(file, blankNonTemplate(source)), ...scanScripts(file, source)];
}

function blankNonTemplate(source: string): string {
  return source
    .replace(SCRIPT_OR_STYLE, keepNewlines)
    .replace(HTML_COMMENT, keepNewlines);
}

function keepNewlines(text: string): string {
  return text.replace(/[^\n]/g, " ");
}

function isCopy(text: string): boolean {
  return /\p{L}/u.test(text);
}

function attributeKey(name: string): string {
  return name.toLowerCase().replaceAll("-", "");
}

function lineAt(source: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index; i += 1) {
    if (source[i] === "\n") line += 1;
  }
  return line;
}

function report(file: string, source: string, index: number, literal: string): string | null {
  const trimmed = literal.replace(/\s+/g, " ").trim();
  if (!isCopy(trimmed)) return null;
  return `${file}:${lineAt(source, index)}:${trimmed}`;
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

function precedingNonSpace(source: string, index: number): string {
  let i = index - 1;
  while (i >= 0 && /\s/.test(source[i] ?? "")) i -= 1;
  return i < 0 ? "" : source[i] ?? "";
}

function isComparand(source: string, stringStart: number): boolean {
  if (stringStart === 0) return false;
  return COMPARAND_PREFIX.test(source.slice(0, stringStart).trimEnd());
}

function hasWord(text: string): boolean {
  return /\p{L}{2,}/u.test(text);
}

function skipSpaceBack(source: string, index: number): number {
  let i = index;
  while (i >= 0 && /\s/.test(source[i] ?? "")) i -= 1;
  return i;
}

function isPathOrUrl(value: string): boolean {
  const trimmed = value.trim();
  if (PATH_OR_URL.test(trimmed)) return true;
  return !/\s/.test(trimmed) && trimmed.includes("/");
}

function isCssSelector(value: string): boolean {
  return /\[(?:data-|class|id|aria-|role\b)/.test(value) || /:[a-z-]+\(/.test(value);
}

function isDisplayFieldValue(source: string, stringStart: number): boolean {
  let i = skipSpaceBack(source, stringStart - 1);
  if (source[i] !== ":") return false;
  i = skipSpaceBack(source, i - 1);
  const end = i + 1;
  while (i >= 0 && /[\w$-]/.test(source[i] ?? "")) i -= 1;
  return DISPLAY_FIELDS.has(attributeKey(source.slice(i + 1, end)));
}

function identifierBeforeCall(source: string, openParen: number): string {
  let i = skipSpaceBack(source, openParen - 1);
  if (source[i] === ">") {
    let depth = 1;
    i -= 1;
    while (i >= 0 && depth > 0) {
      if (source[i] === ">") depth += 1;
      else if (source[i] === "<") depth -= 1;
      i -= 1;
    }
    i = skipSpaceBack(source, i);
  }
  const end = i + 1;
  while (i >= 0 && /[\w$]/.test(source[i] ?? "")) i -= 1;
  return source.slice(i + 1, end);
}

function enclosingCallOpen(source: string, stringStart: number): number | null {
  let i = stringStart - 1;
  let depth = 0;
  while (i >= 0) {
    const character = source[i];
    if (character === "'" || character === '"' || character === "`") {
      let start = i - 1;
      while (start >= 0) {
        const quote = source[start];
        if ((quote === "'" || quote === '"' || quote === "`") && skipJsQuoted(source, start) === i + 1) {
          i = start - 1;
          break;
        }
        start -= 1;
      }
      if (start < 0) return null;
      continue;
    }
    if (character === ")") depth += 1;
    else if (character === "(") {
      if (depth === 0) return i;
      depth -= 1;
    }
    i -= 1;
  }
  return null;
}

function reachesDisplaySink(source: string, stringStart: number): boolean {
  const previous = precedingNonSpace(source, stringStart);
  if (previous !== "(" && previous !== ",") return false;
  const open = enclosingCallOpen(source, stringStart);
  if (open === null) return false;
  return DISPLAY_SINKS.has(identifierBeforeCall(source, open));
}

function isCallArgument(source: string, stringStart: number): boolean {
  const previous = precedingNonSpace(source, stringStart);
  return previous === "(" || previous === ",";
}

function scriptLiteralIsDisplay(script: string, stringStart: number, value: string): boolean {
  const trimmed = value.replace(/\s+/g, " ").trim();
  if (!hasWord(trimmed)) return false;
  if (isPathOrUrl(trimmed)) return false;
  if (isCssSelector(trimmed)) return false;
  if (isDisplayFieldValue(script, stringStart)) return true;
  return reachesDisplaySink(script, stringStart);
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

function scanScripts(file: string, source: string): string[] {
  return scriptBodies(source).flatMap(({ start, body }) =>
    scanScriptBody(file, source, start, body)
  );
}

function scanScriptBody(file: string, source: string, bodyStart: number, body: string): string[] {
  const found: string[] = [];
  let i = 0;
  while (i < body.length) {
    const character = body[i];
    if (character === "'" || character === '"') {
      const end = skipJsQuoted(body, i);
      const value = body.slice(i + 1, end - 1);
      if (scriptLiteralIsDisplay(body, i, value)) {
        const reported = report(file, source, bodyStart + i + 1, value);
        if (reported !== null) found.push(reported);
      }
      i = end;
      continue;
    }
    if (character === "`") {
      found.push(...copyInScriptTemplate(file, source, bodyStart, body, i));
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
    i += 1;
  }
  return found;
}

function copyInScriptTemplate(
  file: string,
  source: string,
  bodyStart: number,
  body: string,
  start: number
): string[] {
  const found: string[] = [];
  let i = start + 1;
  let quasiStart = i;
  while (i < body.length) {
    if (body[i] === "\\") {
      i += 2;
      continue;
    }
    if (body[i] === "`") {
      const value = body.slice(quasiStart, i);
      if (scriptLiteralIsDisplay(body, start, value)) {
        const reported = report(file, source, bodyStart + quasiStart, value);
        if (reported !== null) found.push(reported);
      }
      return found;
    }
    if (body[i] === "$" && body[i + 1] === "{") {
      const value = body.slice(quasiStart, i);
      if (scriptLiteralIsDisplay(body, start, value)) {
        const reported = report(file, source, bodyStart + quasiStart, value);
        if (reported !== null) found.push(reported);
      }
      i = matchingBrace(body, i + 1);
      quasiStart = i;
      continue;
    }
    i += 1;
  }
  return found;
}

function isDisplayMustache(body: string): boolean {
  const trimmed = body.trimStart();
  if (trimmed.startsWith("@html")) return true;
  return trimmed === "" || !/^[#/:@]/.test(trimmed);
}

function copyInExpression(file: string, source: string, exprStart: number, expr: string): string[] {
  const found: string[] = [];
  let i = 0;
  while (i < expr.length) {
    const character = expr[i];
    if (character === "'" || character === '"') {
      const absolute = exprStart + i;
      const end = skipJsQuoted(expr, i);
      const value = expr.slice(i + 1, end - 1);
      if (!isComparand(expr, i) && (!isCallArgument(expr, i) || reachesDisplaySink(expr, i))) {
        const reported = report(file, source, absolute, value);
        if (reported !== null) found.push(reported);
      }
      i = end;
      continue;
    }
    if (character === "`") {
      found.push(...copyInTemplateLiteral(file, source, exprStart, expr, i));
      i = skipJsQuoted(expr, i);
      continue;
    }
    if (character === "/" && expr[i + 1] === "/") {
      const newline = expr.indexOf("\n", i);
      i = newline === -1 ? expr.length : newline;
      continue;
    }
    if (character === "/" && expr[i + 1] === "*") {
      const close = expr.indexOf("*/", i + 2);
      i = close === -1 ? expr.length : close + 2;
      continue;
    }
    i += 1;
  }
  return found;
}

function copyInTemplateLiteral(
  file: string,
  source: string,
  exprStart: number,
  expr: string,
  start: number
): string[] {
  const found: string[] = [];
  let i = start + 1;
  let quasiStart = i;
  while (i < expr.length) {
    if (expr[i] === "\\") {
      i += 2;
      continue;
    }
    if (expr[i] === "`") {
      const reported = report(file, source, exprStart + quasiStart, expr.slice(quasiStart, i));
      if (reported !== null) found.push(reported);
      return found;
    }
    if (expr[i] === "$" && expr[i + 1] === "{") {
      const reported = report(file, source, exprStart + quasiStart, expr.slice(quasiStart, i));
      if (reported !== null) found.push(reported);
      i = matchingBrace(expr, i + 1);
      quasiStart = i;
      continue;
    }
    i += 1;
  }
  return found;
}

function scanMustache(file: string, source: string, open: number): { end: number; found: string[] } {
  const end = matchingBrace(source, open);
  const body = source.slice(open + 1, end - 1);
  if (!isDisplayMustache(body)) return { end, found: [] };
  return { end, found: copyInExpression(file, source, open + 1, body) };
}

function scanTag(file: string, source: string, open: number): { end: number; found: string[] } {
  const found: string[] = [];
  let i = open + 1;
  if (source[i] === "/") {
    const close = source.indexOf(">", i);
    return { end: close === -1 ? source.length : close + 1, found };
  }
  while (i < source.length && /[A-Za-z0-9:-]/.test(source[i] ?? "")) i += 1;
  while (i < source.length) {
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (i >= source.length) break;
    if (source[i] === ">") return { end: i + 1, found };
    if (source[i] === "/" && source[i + 1] === ">") return { end: i + 2, found };
    if (source[i] === "{") {
      const mustache = scanMustache(file, source, i);
      found.push(...mustache.found);
      i = mustache.end;
      continue;
    }
    ATTRIBUTE_NAME.lastIndex = i;
    const name = ATTRIBUTE_NAME.exec(source);
    if (name === null) {
      i += 1;
      continue;
    }
    i = ATTRIBUTE_NAME.lastIndex;
    const visible = VISIBLE_ATTRIBUTE_KEYS.has(attributeKey(name[0]));
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (source[i] !== "=") continue;
    i += 1;
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (source[i] === "'" || source[i] === '"') {
      const close = skipHtmlQuoted(source, i);
      if (visible) {
        const reported = report(file, source, i + 1, source.slice(i + 1, close - 1));
        if (reported !== null) found.push(reported);
      }
      i = close;
      continue;
    }
    if (source[i] === "{") {
      const mustache = scanMustache(file, source, i);
      if (visible) found.push(...mustache.found);
      i = mustache.end;
    }
  }
  return { end: source.length, found };
}

function scanTemplate(file: string, source: string): string[] {
  const found: string[] = [];
  let i = 0;
  while (i < source.length) {
    const character = source[i];
    if (character === "{") {
      const mustache = scanMustache(file, source, i);
      found.push(...mustache.found);
      i = mustache.end;
      continue;
    }
    if (character === "<") {
      const tag = scanTag(file, source, i);
      found.push(...tag.found);
      i = tag.end;
      continue;
    }
    let end = i + 1;
    while (end < source.length && source[end] !== "<" && source[end] !== "{") end += 1;
    const reported = report(file, source, i, source.slice(i, end));
    if (reported !== null) found.push(reported);
    i = end;
  }
  return found;
}

describe("terms rendered by a room's template or passed to a display sink come from that surface's copy owner", () => {
  it("reports the file, line, and literal for a hand-written heading", () => {
    expect(unownedCopyIn("pages/Example.svelte", "<h1>Workbench</h1>")).toEqual([
      "pages/Example.svelte:1:Workbench"
    ]);
  });

  it("ignores owned interpolations, comparands, and structural glyphs", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        [
          "<h1>{wrapDisplayCopy(workbenchPageCopy.title)}</h1>",
          "<span>{wrapDisplayCopy(row.move)} →</span>",
          '{#if kind === "failed"}<b>{count}</b>{/if}',
          "<time>{ageLabel(row.activityAt, now, \"ago\")}</time>"
        ].join("\n")
      )
    ).toEqual([]);
  });

  it("treats a visible attribute and a fallback string as terms", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        '<input aria-label="Add a model" /><p>{title ?? "Event invalid"}</p><h1>{wrapDisplayCopy("retry")}</h1>'
      )
    ).toEqual([
      "pages/Example.svelte:1:Add a model",
      "pages/Example.svelte:1:Event invalid",
      "pages/Example.svelte:1:retry"
    ]);
  });

  it("treats a seals attribute as a visible term", () => {
    expect(
      unownedCopyIn("pages/Example.svelte", '<x seals="exactly these output bytes" />')
    ).toEqual(["pages/Example.svelte:1:exactly these output bytes"]);
  });

  it("reports a script literal passed to a display sink", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        ["<script>", '  const heading = wrapDisplayCopy("Workbench");', "</script>", "<h1>{heading}</h1>"].join(
          "\n"
        )
      )
    ).toEqual(["pages/Example.svelte:2:Workbench"]);
  });

  it("reports a lower-case one-word script literal passed to a display sink", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        ["<script>", '  const action = wrapDisplayCopy("retry");', "</script>", "<button>{action}</button>"].join(
          "\n"
        )
      )
    ).toEqual(["pages/Example.svelte:2:retry"]);
  });

  it("does not report a lower-case one-word script literal passed to an unlisted callee", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        ["<script>", '  const root = querySelector("main");', "</script>", "<div>{root}</div>"].join("\n")
      )
    ).toEqual([]);
  });

  it("reports a script literal sitting in a label field", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        [
          "<script>",
          '  const choice = { value: "x", label: "Unavailable saved model" };',
          "</script>",
          "<span>{choice.label}</span>"
        ].join("\n")
      )
    ).toEqual(["pages/Example.svelte:2:Unavailable saved model"]);
  });

  it("does not report a thrown string just because the script shows error.message", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        [
          "<script>",
          "  let failure = null;",
          "  function load() {",
          "    try { throw new Error(\"The durable event stream could not start.\"); }",
          "    catch (error) { failure = error instanceof Error ? error.message : \"\"; }",
          "  }",
          "</script>",
          "<p>{failure}</p>"
        ].join("\n")
      )
    ).toEqual([]);
  });

  it("reports a thrown-path message only when it is passed to a display sink", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        [
          "<script>",
          "  let failure = null;",
          "  function load() {",
          "    try { throw new Error(\"profile cursor repeated\"); }",
          "    catch (error) { failure = humanErrorMessage(error, \"The durable event stream could not start.\"); }",
          "  }",
          "</script>",
          "<p>{failure}</p>"
        ].join("\n")
      )
    ).toEqual(["pages/Example.svelte:5:The durable event stream could not start."]);
  });

  it("reports a word-bearing format fragment passed to a display sink", () => {
    const source = [
      "<script>",
      "  function heading(name) { return wrapDisplayCopy(`${owned} for ${name}`); }",
      "</script>",
      "<span>{heading(item)}</span>"
    ].join("\n");
    expect(source).toContain("${owned} for ${name}");
    expect(unownedCopyIn("pages/Example.svelte", source)).toEqual([
      "pages/Example.svelte:2:for"
    ]);
  });

  it("does not report a swallowed throw, import, comparand, key, path, or protocol token", () => {
    expect(
      unownedCopyIn(
        "pages/Example.svelte",
        [
          "<script>",
          '  import { copy } from "../lib/exampleCopy";',
          '  const kind = "unavailable";',
          "  try {",
          '    if (kind === "unavailable") throw new Error("profile cursor repeated");',
          "  } catch {",
          "    title = copy.unavailable;",
          "  }",
          '  const tabs = { result: "result" };',
          '  navigate("/atelier/settings");',
          '  matchMedia("(max-width: 48rem)");',
          "</script>",
          "<h1>{title}</h1>"
        ].join("\n")
      )
    ).toEqual([]);
  });

  it("follows each room page's .svelte imports into the components those rooms host", () => {
    expect(roomTemplates()).toEqual(
      expect.arrayContaining([
        "pages/WorkbenchPage.svelte",
        "pages/CatalogPage.svelte",
        "pages/HistoryPage.svelte",
        "pages/RunCockpitPage.svelte",
        "pages/SettingsPage.svelte",
        "pages/WorkflowDetailPage.svelte",
        "components/ReadState.svelte",
        "components/ProblemNotice.svelte",
        "components/CatalogTile.svelte",
        "components/PinnedDecision.svelte",
        "components/V3RunView.svelte",
        "components/NodeDetailPanel.svelte",
        "components/AttemptTranscript.svelte",
        "components/V3AnswerCard.svelte",
        "components/RunCancelCard.svelte",
        "components/ReadableResult.svelte",
        "components/InfoHint.svelte",
        "components/StateMark.svelte",
        "components/BackLink.svelte",
        "components/ProofAnchor.svelte",
        "components/CatalogImportSheet.svelte",
        "components/WorkflowStartSheet.svelte",
        "components/WorkflowGraphDrawing.svelte",
        "components/WorkflowNodePreviewPanel.svelte"
      ])
    );
  });

  it("proves(a-surfaces-terms-come-from-one-source): terms rendered by a room's template or passed to a display sink come from that surface's copy owner", () => {
    const templates = roomTemplates();
    expect(templates).toEqual(expect.arrayContaining(["pages/WorkbenchPage.svelte"]));
    const violations = templates.flatMap((relativePath) =>
      unownedCopyIn(relativePath, readFileSync(resolve(FRONTEND_SRC, relativePath), "utf8"))
    );
    expect(violations, violations.join("\n")).toEqual([]);
  });
});
