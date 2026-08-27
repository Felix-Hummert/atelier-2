import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { FRONTEND_SRC, svelteSourcesIn } from "../support/workshopSources";

/**
 * REQ-UIQ-03: terms of a surface come from one source. Room pages render
 * through `wrapDisplayCopy` and the `*Copy` / shared display-copy owners;
 * a hand-written operator-facing literal in those templates is a second
 * source. Structural glyphs and whitespace are not terms.
 *
 * The file list is the page half of the skin-token walker, plus the Run
 * view body `V3RunView.svelte` that RunCockpitPage hosts.
 */

const RUN_VIEW = "components/V3RunView.svelte";

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
  "title"
]);

const SCRIPT_OR_STYLE = /<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi;
const HTML_COMMENT = /<!--[\s\S]*?-->/g;
const ATTRIBUTE_NAME = /[A-Za-z_:][\w:.-]*/y;
const COMPARAND_PREFIX = /(?:===|!==|==|!=|case)$/;
const TOKEN_ARGUMENT = /^[a-z]+(?:-[a-z]+)*$/;

function roomTemplates(): string[] {
  return [...svelteSourcesIn("pages"), RUN_VIEW];
}

function unownedCopyIn(file: string, source: string): string[] {
  return scanTemplate(file, blankNonTemplate(source));
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

function isTokenArgument(source: string, stringStart: number, value: string): boolean {
  if (!TOKEN_ARGUMENT.test(value.trim())) return false;
  const previous = precedingNonSpace(source, stringStart);
  return previous === "(" || previous === ",";
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
      if (!isComparand(expr, i) && !isTokenArgument(expr, i, value)) {
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

describe("a surface's terms come from one copy owner", () => {
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
        '<input aria-label="Add a model" /><p>{title ?? "Event invalid"}</p><h1>{wrapDisplayCopy("Workbench")}</h1>'
      )
    ).toEqual([
      "pages/Example.svelte:1:Add a model",
      "pages/Example.svelte:1:Event invalid",
      "pages/Example.svelte:1:Workbench"
    ]);
  });

  it("proves(a-surfaces-terms-come-from-one-source): every room template takes its terms from that surface's copy owner", () => {
    const templates = roomTemplates();
    expect(templates).toEqual(expect.arrayContaining(["pages/WorkbenchPage.svelte", RUN_VIEW]));
    const violations = templates.flatMap((relativePath) =>
      unownedCopyIn(relativePath, readFileSync(resolve(FRONTEND_SRC, relativePath), "utf8"))
    );
    expect(violations, violations.join("\n")).toEqual([]);
  });
});
