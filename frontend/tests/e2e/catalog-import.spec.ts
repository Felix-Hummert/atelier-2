import { expect, test, type Page } from "@playwright/test";

import { catalogPageCopy } from "../../src/lib/catalogPageCopy";

/**
 * The operator's own journey through the import doors (#659).
 *
 * "I have no workflow and cannot build one" was the report this room answers,
 * so the proof is the report's own path: open the Catalog, put a file in, see
 * it listed, admit it, and read that it is startable. Everything below happens
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

async function importInto(page: Page, label: string, document: string): Promise<void> {
  const door = page.getByLabel(label);
  await door.fill(document);
  await door
    .locator("xpath=ancestor::section[1]")
    .getByRole("button", { name: catalogPageCopy.importAction })
    .click();
}

test("proves(the-operator-imports-a-workflow-and-an-agent-and-starts-what-was-imported): the catalog import doors carry a file from disk to startable", async ({
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

  await importInto(page, catalogPageCopy.importWorkflowLabel, workflowFile(schemaHash, undefined, workflowName));
  await expect(entry(page, workflowName)).toBeVisible();
  await expect(entry(page, workflowName).getByRole("button", { name: catalogPageCopy.admit })).toBeVisible();
  await expect(entry(page, workflowName).getByText(catalogPageCopy.provenanceManual)).toBeVisible();

  await importInto(page, catalogPageCopy.importAgentLabel, agentFile(agentName));
  await expect(entry(page, agentName)).toBeVisible();
  // An imported agent belongs to the provider whose format it arrived in, and
  // the row says so instead of implying it runs anywhere.
  await expect(entry(page, agentName).getByText(catalogPageCopy.agentProviderClaude)).toBeVisible();

  await entry(page, workflowName).getByRole("button", { name: catalogPageCopy.admit }).click();
  await expect(
    entry(page, workflowName).getByRole("button", { name: catalogPageCopy.admit })
  ).toHaveCount(0);

  // The admission is durable, not a screen state: a cold load of the room
  // reads the same verdict back out of the catalog.
  await page.reload();
  await expect(entry(page, workflowName).getByRole("link", { name: "Details" })).toBeVisible();

  await entry(page, workflowName).getByRole("link", { name: "Details" }).click();
  await expect(page.getByRole("heading", { name: workflowName })).toBeVisible();
});

test("an unadmitted sibling of an admitted name shows as a newer revision, not a second card", async ({
  page
}, testInfo) => {
  const workflowName = scenarioName("catalog-lineage-proof", testInfo.repeatEachIndex);
  const schemaHash = await anyJsonSchema(page);

  await page.goto("/atelier/catalog");
  await importInto(
    page,
    catalogPageCopy.importWorkflowLabel,
    workflowFile(schemaHash, undefined, workflowName)
  );
  await entry(page, workflowName).getByRole("button", { name: catalogPageCopy.admit }).click();
  await expect(entry(page, workflowName).getByRole("button", { name: catalogPageCopy.admit })).toHaveCount(0);

  // A second, unadmitted revision is published under the same name -- the
  // live duplicate-card finding (#659): the room must not draw a second
  // "catalog-lineage-proof" card for it.
  await importInto(
    page,
    catalogPageCopy.importWorkflowLabel,
    workflowFile(schemaHash, "Is the door still open?", workflowName)
  );

  await expect(page.getByRole("listitem").filter({ hasText: workflowName })).toHaveCount(1);
  const stateHint = entry(page, workflowName).getByRole("button", { name: catalogPageCopy.stateHint });
  await expect(stateHint).toBeVisible();
  await stateHint.click();
  await expect(entry(page, workflowName).getByRole("status")).toHaveText(
    catalogPageCopy.newerRevisionHint
  );
});

test("the catalog names the refusal the API gave instead of guessing at one", async ({ page }) => {
  await page.goto("/atelier/catalog");
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();

  await importInto(
    page,
    catalogPageCopy.importAgentLabel,
    "---\nname: nameless\ndescription: Has a key nobody knows.\ncolor: cyan\n---\n\nBody.\n"
  );

  await expect(page.getByText("Invalid agent definition document")).toBeVisible();
  await expect(page.getByText("field-unknown: color")).toBeVisible();
});

test("the catalog room is composed, not squeezed, at 390 pixels", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/catalog");
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();

  await expect(page.getByRole("navigation", { name: "Workshop" })).toBeVisible();
});
