import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import { shortFingerprint } from "../../src/lib/fingerprint";

async function publishSchema(page: Page, document: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: document
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

const anyJsonSchema = (page: Page): Promise<string> => publishSchema(page, "true");

function declaredOutput(schemaHash: string, name = "result"): string[] {
  return [
    "    outputs:",
    `      - name: ${name}`,
    "        schema:",
    `          ref: ${name}-schema`,
    `          revision: ${schemaHash}`
  ];
}

async function publishYaml(page: Page, yaml: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: yaml
  });
  expect(published.status()).toBe(201);
  return (await published.json()).workflow_revision_hash as string;
}

test("the picker names refusals by shape and collapses onto a chosen start", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const readyName = "picker-ready-345";
  const unlistedName = "unlisted-345";
  const unnamableName = "Der Picker auf 345";
  const readyHash = await publishYaml(
    page,
    [
      "format_version: 3",
      `name: ${readyName}`,
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Start from the collapsed picker.",
      ...declaredOutput(schemaHash),
      ""
    ].join("\n")
  );
  await publishYaml(
    page,
    [
      "format_version: 3",
      `name: ${unlistedName}`,
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Published and never admitted.",
      ...declaredOutput(schemaHash),
      ""
    ].join("\n")
  );
  await publishYaml(
    page,
    [
      "format_version: 3",
      `name: ${unnamableName}`,
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: This title cannot be a catalog name.",
      ""
    ].join("\n")
  );
  const founded = await page.request.post(`${api}/workflow-lineages`, {
    data: {
      workflow_revision_hash: readyHash,
      actor: "e2e",
      activated_at: "2026-08-18T00:00:00Z"
    }
  });
  expect(founded.status()).toBe(201);

  await page.goto("/atelier/new");
  const ready = page.getByRole("article", { name: readyName });
  const unlisted = page.getByRole("article", { name: unlistedName });
  const unnamable = page.getByRole("article", { name: unnamableName });
  await expect(ready).toHaveAttribute("data-catalog-form", "ready");
  await expect(unlisted).toHaveAttribute("data-catalog-form", "unlisted");
  await expect(unlisted).toContainText("Unlisted");
  await expect(unnamable).toHaveAttribute("data-catalog-form", "refused");
  await expect(unnamable).toContainText("Unnamable");
  await expect(unnamable.getByRole("radio")).toBeDisabled();
  await page.screenshot({
    path: "test-results/picker-refusal.png",
    fullPage: true
  });

  await ready.getByRole("radio").click();
  await expect(page.getByRole("heading", { name: "Run ID" })).toBeVisible({
    timeout: 20_000
  });
  await expect(page.getByRole("article", { name: unlistedName })).toHaveCount(0);
  await expect(page.getByRole("article", { name: unnamableName })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Change" })).toBeVisible();
  await page.screenshot({
    path: "test-results/picker-chosen.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "Change" })).toBeVisible();
  await page.screenshot({
    path: "test-results/picker-chosen-390x844.png",
    fullPage: true
  });

  const scan = await new AxeBuilder({ page }).analyze();
  expect(scan.violations, JSON.stringify(scan.violations, null, 2)).toEqual([]);
});

test("the picker names an empty listing", async ({ page }) => {
  // The e2e host seeds two V1 revisions for Found/Absent. This route is the
  // empty listing the picker already renders; it does not pretend the seed
  // is gone.
  await page.route("**/atelier/api/v1/workflow-revisions**", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], next_after_revision_hash: null })
    });
  });
  await page.goto("/atelier/new");
  await expect(page.getByText("No saved workflows yet.")).toBeVisible();
  await page.screenshot({
    path: "test-results/picker-empty.png",
    fullPage: true
  });
});

test("Details shows published substance, an honest empty, and the edit door", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const portions = await page.request.post(`${api}/schema-revisions`, {
    headers: { "content-type": "application/json" },
    data: '{"type":"integer"}'
  });
  expect([200, 201]).toContain(portions.status());
  const portionsHash = (await portions.json()).schema_revision_hash as string;
  const readyName = "details-ready-345";
  const emptyName = "details-empty-345";
  const refusedName = "Der Details auf 345";
  const readyYaml = [
    "format_version: 3",
    `name: ${readyName}`,
    "graph_inputs:",
    "  - name: portions",
    "    schema:",
    "      ref: portions-schema",
    `      revision: ${portionsHash}`,
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Show the published substance.",
    "    inputs:",
    "      - name: portions",
    "        from:",
    "          graph_input: portions",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");
  const readyHash = await publishYaml(page, readyYaml);
  await publishYaml(
    page,
    [
      "format_version: 3",
      `name: ${emptyName}`,
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: No order is declared.",
      ...declaredOutput(schemaHash),
      ""
    ].join("\n")
  );
  await publishYaml(
    page,
    [
      "format_version: 3",
      `name: ${refusedName}`,
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: This title cannot be a catalog name.",
      ""
    ].join("\n")
  );
  expect(
    (
      await page.request.post(`${api}/workflow-lineages`, {
        data: {
          workflow_revision_hash: readyHash,
          actor: "e2e",
          activated_at: "2026-08-18T00:00:00Z"
        }
      })
    ).status()
  ).toBe(201);

  await page.goto("/atelier/new");
  const ready = page.getByRole("article", { name: readyName });
  await ready.getByText("Details", { exact: true }).click();
  // The published nodes are drawn as the quiet pipe now: a shape and a name,
  // with the authored prompt excerpt living on the workflow's own detail page
  // (mockup v5 §04), so the picker no longer repeats it.
  await expect(ready.locator('[data-node-id]').first()).toBeVisible();
  await expect(ready).not.toContainText("Show the published substance.");
  await expect(ready.getByRole("region", { name: "Orders" })).toContainText("portions");
  await expect(ready.getByRole("heading", { name: "Revisions" })).toBeVisible();
  await expect(ready.getByText("One revision.")).toBeVisible();
  // The digest is shown shortened beside its name, never printed whole and
  // never hidden behind a click (operator ruling 23.08.).
  await expect(ready).not.toContainText(readyHash);
  await expect(ready.getByRole("group", { name: "Workflow revision" })).toContainText(
    shortFingerprint(readyHash)
  );
  await expect(ready.getByText("Seals the published document.")).toBeVisible();
  await ready.getByRole("button", { name: "Copy Workflow revision" }).click();
  await page.screenshot({
    path: "test-results/picker-details.png",
    fullPage: true
  });

  const empty = page.getByRole("article", { name: emptyName });
  await empty.getByText("Details", { exact: true }).click();
  await expect(empty.getByText("No orders.")).toBeVisible();
  await expect(empty.getByText("One revision.")).toBeVisible();
  await page.screenshot({
    path: "test-results/picker-details-empty.png",
    fullPage: true
  });

  const refused = page.getByRole("article", { name: refusedName });
  await refused.getByText("Details", { exact: true }).click();
  await expect(refused).toContainText("Unnamable");
  await expect(refused).toContainText("Cannot be started");
  await expect(refused.getByRole("button", { name: "Edit" })).toBeVisible();
  await page.screenshot({
    path: "test-results/picker-details-refusal.png",
    fullPage: true
  });

  await ready.getByRole("button", { name: "Edit" }).click();
  const editor = ready.getByLabel("Exact workflow YAML");
  await expect(editor).toHaveValue(new RegExp(`name: ${readyName}`));
  await editor.fill(readyYaml.replace("Show the published substance.", "Edited substance."));
  await ready.getByRole("button", { name: "Review publication" }).click();
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  const listed = page.getByRole("article", { name: readyName });
  const choice = listed.getByLabel(`Revision of ${readyName}`);
  await expect(choice).toBeVisible({ timeout: 20_000 });
  const head = await page.request.get(`${api}/workflow-revisions/by-name/${readyName}`);
  expect(head.status()).toBe(200);
  const admitted = (await head.json()).workflow_revision_hash as string;
  expect(admitted).not.toBe(readyHash);
  await expect(choice).toHaveValue(admitted);
  await expect(choice.locator(`option[value="${admitted}"]`)).toHaveText("Latest");
});

test("an order whose schema names one required string field is a plain text material, its schema visible", async ({
  page
}) => {
  const schemaHash = await anyJsonSchema(page);
  const messageHash = await publishSchema(
    page,
    JSON.stringify({
      type: "object",
      required: ["message"],
      properties: { message: { type: "string", minLength: 1 } }
    })
  );
  const workflowName = "order-editor-345";
  const workflowHash = await publishYaml(
    page,
    [
      "format_version: 3",
      `name: ${workflowName}`,
      "graph_inputs:",
      "  - name: message",
      "    schema:",
      "      ref: message-schema",
      `      revision: ${messageHash}`,
      "nodes:",
      "  - id: reply",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Reply to the message.",
      "    inputs:",
      "      - name: message",
      "        from:",
      "          graph_input: message",
      ...declaredOutput(schemaHash),
      ""
    ].join("\n")
  );
  expect(workflowHash).not.toBe("");

  await page.goto("/atelier/new");
  const article = page.getByRole("article", { name: workflowName });
  await article.getByRole("radio").click();
  const order = page.getByRole("article", { name: "Order message" });
  await expect(order).toBeVisible();
  const fields = order.getByRole("region", { name: "Fields of message" });
  await expect(fields).toContainText("message");
  await expect(fields).toContainText("string");
  await expect(fields).toContainText("Required");
  const material = order.getByLabel("Material message");
  await expect(material).toHaveJSProperty("tagName", "INPUT");
  await page.screenshot({ path: "test-results/order-editor-empty.png", fullPage: true });

  await material.fill("Please summarize the open runs.");
  await page.screenshot({ path: "test-results/order-editor-filled.png", fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(material).toBeVisible();
  await page.screenshot({ path: "test-results/order-editor-filled-390x844.png", fullPage: true });

  const scan = await new AxeBuilder({ page }).analyze();
  expect(scan.violations, JSON.stringify(scan.violations, null, 2)).toEqual([]);
});

