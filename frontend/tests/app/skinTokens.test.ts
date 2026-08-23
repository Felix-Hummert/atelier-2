import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * REQ-UI-14: structure is separated from skin, and a component consumes tokens
 * only. That promise is worth something only while it holds for every surface
 * — one page keeping its own colour or its own scale is enough for a token
 * edit to stop being a re-skin. So this reads the shipped styles the way a
 * re-skin would.
 */

const sourceRoot = resolve(import.meta.dirname, "../../src");
const stylesheet = "styles.css";

/** Where the skin is declared: the one place a literal belongs. */
const tokenDeclaration = /^\s*--[a-z0-9-]+:/;
/** A breakpoint cannot read a custom property, so `@media` keeps its lengths. */
const mediaPrelude = /^\s*@media\b/;
/** The hairline, and the negative margin that pulls a border back over it. */
const hairline = /^-?1px$/;

const colourLiteral = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(/;
const scaleLiteral = /(?<![\w-])-?\d*\.?\d+(?:rem|em|px)\b/g;

function styleBlocks(source: string, file: string): string[] {
  if (file.endsWith(".css")) {
    return [source];
  }
  return [...source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(([, body]) => body ?? "");
}

/** Prose explains the rules; only the declarations are held to them. */
function withoutComments(block: string): string {
  return block.replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "));
}

function literalsIn(block: string, file: string): string[] {
  return withoutComments(block).split("\n").flatMap((line, index) => {
    if (tokenDeclaration.test(line) || mediaPrelude.test(line)) {
      return [];
    }
    const lengths = [...line.matchAll(scaleLiteral)]
      .map((match) => match[0])
      .filter((length) => !hairline.test(length));
    const found = colourLiteral.test(line) ? [...lengths, "a colour"] : lengths;
    return found.length === 0 ? [] : [`${file}:${index + 1} names ${found.join(", ")}: ${line.trim()}`];
  });
}

function styledSources(): string[] {
  const svelte = ["pages", "components"].flatMap((directory) =>
    readdirSync(resolve(sourceRoot, directory))
      .filter((name) => name.endsWith(".svelte"))
      .map((name) => `${directory}/${name}`)
  );
  return [stylesheet, "App.svelte", ...svelte];
}

function tokenBlock(source: string, opener: string): string {
  const start = source.indexOf(opener);
  expect(start, `the stylesheet declares ${opener}`).toBeGreaterThan(-1);
  return source.slice(start, source.indexOf("\n}", start));
}

function declaredTokens(block: string): string[] {
  return [...block.matchAll(/^\s*(--[a-z0-9-]+):/gm)].map((match) => match[1] ?? "");
}

function colourTokens(block: string): string[] {
  return [...block.matchAll(/^\s*(--[a-z0-9-]+):\s*(.+);$/gm)]
    .filter((match) => colourLiteral.test(match[2] ?? ""))
    .map((match) => match[1] ?? "")
    .sort();
}

describe("the skin lives in one place", () => {
  it.each(styledSources())("%s draws itself from tokens alone", (relativePath) => {
    const source = readFileSync(resolve(sourceRoot, relativePath), "utf8");
    const literals = styleBlocks(source, relativePath).flatMap((block) =>
      literalsIn(block, relativePath)
    );
    expect(literals).toEqual([]);
  });

  it("answers every light colour with a dark one of its own", () => {
    const source = readFileSync(resolve(sourceRoot, stylesheet), "utf8");
    const light = colourTokens(tokenBlock(source, ":root {"));
    const dark = declaredTokens(tokenBlock(source, "@media (prefers-color-scheme: dark)"));
    expect(light.length).toBeGreaterThan(0);
    // A dark answer may name another token rather than repeat a literal, so
    // what is checked is that dark answers at all, not how it spells it.
    expect(light.filter((token) => !dark.includes(token))).toEqual([]);
  });
});
