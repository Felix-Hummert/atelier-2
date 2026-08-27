import { expect, test, type Page } from "@playwright/test";

import { catalogPageCopy } from "../../src/lib/catalogPageCopy";

/**
 * The operator's own journey through the import doors (#659).
 *
 * "I have no workflow and cannot build one" was the report this room answers,
 * so the proof is the report's own path: open the Catalog, put a file in, see
 * it listed and read that it is startable. Everything below happens
 * through the browser — the only two API calls stage a schema the workflow
 * pins, which no surface publishes and no part of this journey is about.
 */

function scenarioName(stem: string, repetition: number): string {
  return `${stem}-${repetition}`;
}

function agentFile(name: string): string {
  return [
    "---",
    `name: ${name}`,
    "description: Proves an authored agent file reaches the catalog.",
    "model: sonnet",
    "tools: Read, Grep",
    "---",
    "",
    "You write down exactly what the stage asks for.",
    ""
  ].join("\n");
}

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

function workflowFile(
  schemaHash: string,
  promptText: string | undefined,
  name: string
): string {
  return [
    "format_version: 3",
    `name: ${name}`,
    "nodes:",
    "  - id: ask",
    "    type: wait",
    `    prompt: ${promptText ?? "Is the import door open?"}`,
    "    outputs:",
    "      - name: answer",
    "        schema:",
    "          ref: answer-schema",
    `          revision: ${schemaHash}`,
    ""
  ].join("\n");
}

/** The entry the catalog draws for one published name. */
function entry(page: Page, name: string) {
  return page.getByRole("listitem").filter({ hasText: name });
}

async function importInto(page: Page, fileName: string, document: string): Promise<void> {
  await page.getByRole("button", { name: catalogPageCopy.import }).click();
  await expect(page.getByRole("dialog", { name: catalogPageCopy.import })).toHaveCount(0);
  await page.getByLabel(catalogPageCopy.filePicker).setInputFiles({
    name: fileName,
    mimeType: "application/octet-stream",
    buffer: Buffer.from(document)
  });
  await page.getByRole("button", { name: catalogPageCopy.addToCatalog }).click();
}

test("proves(the-operator-imports-a-workflow-and-an-agent-and-starts-what-was-imported): the catalog import sheet carries a file from disk to startable", async ({
  page
}, testInfo) => {
  const workflowName = scenarioName("catalog-import-proof", testInfo.repeatEachIndex);
  const agentName = scenarioName("catalog-import-scribe", testInfo.repeatEachIndex);
  const schemaHash = await anyJsonSchema(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier");
  const catalogLink = page.getByRole("navigation", { name: "Workshop" }).getByRole("link", { name: "Catalog" });
  await catalogLink.click();
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();
  await expect(catalogLink).toBeInViewport();
  await expect(page.getByText(catalogPageCopy.skillsNone)).toBeVisible();
  await expect(entry(page, workflowName)).toHaveCount(0);
  await expect(entry(page, agentName)).toHaveCount(0);

  await importInto(page, `${workflowName}.yaml`, workflowFile(schemaHash, undefined, workflowName));
  await expect(entry(page, workflowName)).toBeVisible();
  await expect(entry(page, workflowName).getByText(catalogPageCopy.provenanceManual)).toBeVisible();

  await importInto(page, `${agentName}.agent.md`, agentFile(agentName));
  await expect(entry(page, agentName)).toBeVisible();
  // An imported agent belongs to the provider whose format it arrived in, and
  // the row says so instead of implying it runs anywhere.
  await expect(entry(page, agentName).getByText(catalogPageCopy.agentProviderClaude)).toBeVisible();

  // The admission is durable, not a screen state: a cold load of the room
  // reads the admitted entry back and offers its enabled manual start door.
  await page.reload();
  await expect(entry(page, workflowName).getByRole("button", { name: catalogPageCopy.stateHint })).toHaveCount(0);
  await expect(page.getByText(catalogPageCopy.notAdmittedHint)).toHaveCount(0);
  await entry(page, workflowName).getByRole("link", { name: workflowName }).click();
  await expect(page.getByRole("heading", { name: workflowName })).toBeVisible();
  await expect(page.getByRole("button", { name: catalogPageCopy.start })).toBeEnabled();
});

test("an unadmitted sibling of an admitted name shows as a newer revision, not a second card", async ({
  page
}, testInfo) => {
  const workflowName = scenarioName("catalog-lineage-proof", testInfo.repeatEachIndex);
  const schemaHash = await anyJsonSchema(page);

  await page.goto("/atelier/catalog");
  await importInto(page, `${workflowName}.yaml`, workflowFile(schemaHash, undefined, workflowName));

  // A second, unadmitted revision is published under the same name -- the
  // live duplicate-card finding (#659): the room must not draw a second
  // "catalog-lineage-proof" card for it.
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: workflowFile(schemaHash, "Is the door still open?", workflowName)
  });
  expect([200, 201]).toContain(published.status());
  await page.reload();

  await expect(page.getByRole("listitem").filter({ hasText: workflowName })).toHaveCount(1);
  const stateHint = entry(page, workflowName).getByRole("button", { name: catalogPageCopy.stateHint });
  await expect(stateHint).toBeVisible();
  await stateHint.click();
  await expect(entry(page, workflowName).getByRole("status")).toHaveText(
    catalogPageCopy.newerRevisionHint
  );
});

test("the catalog names an unrecognized file without adding it", async ({ page }) => {
  await page.goto("/atelier/catalog");
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();

  await page.getByLabel(catalogPageCopy.filePicker).setInputFiles({
    name: "nameless.agent.md",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("---\nname: nameless\ndescription: Has a key nobody knows.\ncolor: cyan\n---\n\nBody.\n")
  });

  await expect(page.getByText(catalogPageCopy.unrecognized)).toBeVisible();
  await expect(page.getByRole("button", { name: catalogPageCopy.close })).toBeVisible();
  await expect(page.getByText("Choose a file", { exact: true })).toHaveCount(0);
});

test("the catalog room is composed, not squeezed, at 390 pixels", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/catalog");
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();

  const navigation = page.getByRole("navigation", { name: "Workshop" });
  await expect(navigation).toBeVisible();
  const roomBoxes = await Promise.all([
    navigation.getByRole("link", { name: "Workbench" }).boundingBox(),
    navigation.getByRole("link", { name: "Catalog" }).boundingBox(),
    navigation.getByRole("link", { name: "History" }).boundingBox(),
    navigation.getByRole("link", { name: /Settings/ }).boundingBox()
  ]);
  expect(roomBoxes.every((box) => box !== null)).toBe(true);
  const roomRows = roomBoxes.flatMap((box) => box === null ? [] : [box.y]);
  expect(Math.max(...roomRows) - Math.min(...roomRows)).toBeLessThanOrEqual(1);
  const importBox = await page.getByRole("button", { name: catalogPageCopy.import }).boundingBox();
  expect(importBox).not.toBeNull();
  expect((importBox?.x ?? 0) + (importBox?.width ?? 0)).toBeLessThanOrEqual(390);
});
