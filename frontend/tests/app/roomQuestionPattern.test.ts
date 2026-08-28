import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  FRONTEND_SRC,
  svelteImportClosure,
  svelteSourcesIn
} from "../support/workshopSources";

/**
 * REQ-UIQ-07: a question has a pattern, and the pattern is a reused
 * component. The workshop's question surfaces in this tree are the
 * `*Sheet.svelte` files (`dialog.sheet` under `.sheet-positioner`),
 * `RunForkSheet` (a root `dialog`), `V3AnswerCard` (`section.decision`),
 * `PinnedDecision` (`section.pinned-decision`), `HumanActionCard` and
 * `ReconciliationActionCard` (`section.human-action`), and
 * `RunCancelCard` (`section.cancel`). A room page or a host such as
 * `V3RunView` asks by importing one of those, not by composing a
 * `<dialog>` or a decision stage next to another surface.
 *
 * The file list is every room page and every `.svelte` file those pages
 * import, transitively. A script-built overlay, a standing Workbench
 * ear, and whether those several question files share one Stage or
 * Sheet module are outside what the tags show.
 */

const SCRIPT_OR_STYLE = /<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi;
const HTML_COMMENT = /<!--[\s\S]*?-->/g;
const ATTRIBUTE_NAME = /[A-Za-z_:][\w:.-]*/y;

/** CSS class tokens the existing question components put on their surface. */
const QUESTION_SURFACE_CLASSES = new Set([
  "cancel",
  "decision",
  "human-action",
  "pinned-decision",
  "sheet-positioner"
]);

interface StartTag {
  name: string;
  classes: string[];
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

function classTokens(raw: string): string[] {
  return raw.split(/\s+/).filter((token) => token.length > 0);
}

function hasQuestionSurfaceClass(classes: readonly string[]): boolean {
  return classes.some((token) => QUESTION_SURFACE_CLASSES.has(token));
}

function isQuestionMarkup(tag: StartTag): boolean {
  if (!isHtmlTag(tag.name)) return false;
  if (tag.name === "dialog") return true;
  if (tag.role === "dialog") return true;
  if (tag.name !== "section" && tag.name !== "a") return false;
  return hasQuestionSurfaceClass(tag.classes);
}

function isQuestionOwner(tag: StartTag): boolean {
  if (!isHtmlTag(tag.name)) return false;
  if (tag.name === "dialog") return true;
  if (tag.role === "dialog") return true;
  return hasQuestionSurfaceClass(tag.classes);
}

function report(file: string, source: string, tag: StartTag): string {
  return `${file}:${lineAt(source, tag.index)}:${tag.text}`;
}

function readAttributes(
  source: string,
  start: number
): { classes: string[]; role: string | null; end: number } {
  const classes: string[] = [];
  let role: string | null = null;
  let i = start;
  while (i < source.length) {
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (i >= source.length) break;
    if (source[i] === ">") return { classes, role, end: i + 1 };
    if (source[i] === "/" && source[i + 1] === ">") return { classes, role, end: i + 2 };
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
    if (attribute.startsWith("class:")) {
      const token = attribute.slice("class:".length);
      if (token.length > 0) classes.push(token);
    }
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (source[i] !== "=") continue;
    i += 1;
    while (i < source.length && /\s/.test(source[i] ?? "")) i += 1;
    if (source[i] === "'" || source[i] === '"') {
      const close = skipHtmlQuoted(source, i);
      const value = source.slice(i + 1, close - 1);
      if (attribute === "class") classes.push(...classTokens(value));
      if (attribute === "role") role = value;
      i = close;
      continue;
    }
    if (source[i] === "{") {
      i = matchingBrace(source, i);
    }
  }
  return { classes, role, end: source.length };
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
      tag: { name, classes: attributes.classes, role: attributes.role, index: i, text },
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

function adHocQuestionsIn(file: string, source: string): string[] {
  const template = blankNonTemplate(source);
  const tags = startTags(template);
  const html = tags.filter((tag) => isHtmlTag(tag.name));
  const owner = html[0];
  if (owner !== undefined && isQuestionOwner(owner)) return [];
  return html.filter(isQuestionMarkup).map((tag) => report(file, source, tag));
}

describe("a dialog or decision stage a room's template or imported component opens is that file's own question surface, not markup nested in another surface", () => {
  it("reports the file, line, and opening tag for a dialog nested in a room page", () => {
    expect(
      adHocQuestionsIn(
        "pages/Example.svelte",
        '<section class="surface"><dialog class="sheet" aria-label="Import"></dialog></section>'
      )
    ).toEqual(['pages/Example.svelte:1:<dialog class="sheet" aria-label="Import">']);
  });

  it("reports a role=dialog overlay composed beside another surface", () => {
    expect(
      adHocQuestionsIn(
        "components/Host.svelte",
        '<section class="v3-run"><div role="dialog" class="sheet">Stop this run?</div></section>'
      )
    ).toEqual(['components/Host.svelte:1:<div role="dialog" class="sheet">']);
  });

  it("reports a decision stage copied into a host instead of imported", () => {
    expect(
      adHocQuestionsIn(
        "pages/Example.svelte",
        '<section class="workbench"><section class="pinned-decision"><h2>Ship it?</h2></section></section>'
      )
    ).toEqual(['pages/Example.svelte:1:<section class="pinned-decision">']);
  });

  it("treats an imported question component as the pattern, not as nested markup", () => {
    expect(
      adHocQuestionsIn(
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

  it("treats a sheet whose surface is the dialog as the question component", () => {
    expect(
      adHocQuestionsIn(
        "components/AddModelSheet.svelte",
        '<div class="sheet-positioner"><dialog class="sheet" aria-labelledby="title"></dialog></div>'
      )
    ).toEqual([]);
  });

  it("treats a root dialog as the question component", () => {
    expect(
      adHocQuestionsIn(
        "components/RunForkSheet.svelte",
        '<dialog class="dialog" aria-label="Start again from this node"></dialog>'
      )
    ).toEqual([]);
  });

  it("treats a decision section as the question component, including its own overlay", () => {
    expect(
      adHocQuestionsIn(
        "components/RunCancelCard.svelte",
        [
          '<section class="cancel">',
          '  <button type="button">Stop this run</button>',
          "</section>",
          '{#if confirming}<dialog class="dialog" aria-labelledby="question"></dialog>{/if}'
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

  it("proves(a-question-uses-the-shared-question-pattern): a dialog or decision stage a room's template or imported component opens is that file's own question surface, not markup nested in another surface", () => {
    const templates = roomTemplates();
    expect(templates).toEqual(expect.arrayContaining(["pages/WorkbenchPage.svelte"]));
    const violations = templates.flatMap((relativePath) =>
      adHocQuestionsIn(relativePath, readFileSync(resolve(FRONTEND_SRC, relativePath), "utf8"))
    );
    expect(violations, violations.join("\n")).toEqual([]);
  });
});
