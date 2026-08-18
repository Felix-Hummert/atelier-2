import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

async function publishSchema(page: Page, document: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: document
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).revision_hash as string;
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
  return (await published.json()).revision_hash as string;
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
      revision_hash: readyHash,
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
