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

const WORKFLOW_NAME = "catalog-import-proof";
const AGENT_NAME = "catalog-import-scribe";

const AGENT_FILE = [
  "---",
  `name: ${AGENT_NAME}`,
  "description: Proves an authored agent file reaches the catalog.",
  "model: sonnet",
  "tools: Read, Grep",
  "---",
  "",
  "You write down exactly what the stage asks for.",
  ""
].join("\n");

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

function workflowFile(schemaHash: string): string {
  return [
    "format_version: 3",
    `name: ${WORKFLOW_NAME}`,
    "nodes:",
    "  - id: ask",
    "    type: wait",
    "    prompt: Is the import door open?",
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
}) => {
  const schemaHash = await anyJsonSchema(page);

  await page.goto("/atelier");
  await page.getByRole("navigation", { name: "Workshop" }).getByRole("link", { name: "Catalog" }).click();
  await expect(page.getByRole("heading", { name: catalogPageCopy.title })).toBeVisible();
  await expect(page.getByText(catalogPageCopy.agentsEmpty)).toBeVisible();
  await expect(page.getByText(catalogPageCopy.skillsNone)).toBeVisible();
  await expect(entry(page, WORKFLOW_NAME)).toHaveCount(0);

  await importInto(page, catalogPageCopy.importWorkflowLabel, workflowFile(schemaHash));
  await expect(entry(page, WORKFLOW_NAME)).toBeVisible();
  await expect(entry(page, WORKFLOW_NAME).getByText(catalogPageCopy.notAdmitted)).toBeVisible();
  await expect(entry(page, WORKFLOW_NAME).getByText(catalogPageCopy.provenanceManual)).toBeVisible();

  await importInto(page, catalogPageCopy.importAgentLabel, AGENT_FILE);
  await expect(entry(page, AGENT_NAME)).toBeVisible();
  // An imported agent belongs to the provider whose format it arrived in, and
  // the row says so instead of implying it runs anywhere.
  await expect(entry(page, AGENT_NAME).getByText(catalogPageCopy.agentProviderClaude)).toBeVisible();
  await expect(entry(page, AGENT_NAME).getByText(catalogPageCopy.agentPublishedOnly)).toBeVisible();

  await entry(page, WORKFLOW_NAME).getByRole("button", { name: catalogPageCopy.admit }).click();
  await expect(entry(page, WORKFLOW_NAME).getByText(catalogPageCopy.startable)).toBeVisible();
  await expect(
    entry(page, WORKFLOW_NAME).getByRole("button", { name: catalogPageCopy.admit })
  ).toHaveCount(0);

  // The admission is durable, not a screen state: a cold load of the room
  // reads the same verdict back out of the catalog.
  await page.reload();
  await expect(entry(page, WORKFLOW_NAME).getByText(catalogPageCopy.startable)).toBeVisible();

  // And what the admission is for: the name now resolves in the library the
  // start door reads, which is what "startable" claims.
  const resolved = await page.request.get(
    `/atelier/api/v1/workflow-revisions/by-name/${WORKFLOW_NAME}`
  );
  expect(resolved.status()).toBe(200);
  expect((await resolved.json()).display_name).toBe(WORKFLOW_NAME);
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

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );

  expect(overflow).toBeLessThanOrEqual(0);
});
