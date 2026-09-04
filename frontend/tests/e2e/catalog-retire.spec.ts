import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { catalogPageCopy, workflowDetailCopy } from "../../src/lib/catalogPageCopy";

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

function workflowDocument(name: string, schemaHash: string): string {
  return [
    "format_version: 3",
    `name: ${name}`,
    "nodes:",
    "  - id: build",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Finish this proof.",
    "    outputs:",
    "      - name: result",
    "        schema:",
    "          ref: result-schema",
    `          revision: ${schemaHash}`,
    ""
  ].join("\n");
}

async function checkedAgentConfiguration(page: Page, stem: string): Promise<string> {
  const providerId = "e2e-v3";
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: `${stem}-profile`,
      revision_number: 1,
      provider_id: providerId,
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: `${stem}-model`,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const configurationHash = (await configuration.json()).agent_configuration_revision_hash as string;
  const current = await page.request.get(`/atelier/api/v1/model-registries/${providerId}`);
  const currentRegistry = current.status() === 200
    ? await current.json() as {
        revision_number: number;
        entries: Array<{ model_id: string; agent_configuration_revision_hash: string }>;
      }
    : null;
  expect([200, 404]).toContain(current.status());
  // The registry's read resource carries fields its write resource refuses,
  // so entries this spec keeps are projected back down to what a PUT accepts.
  const keptEntries = (currentRegistry?.entries ?? [])
    .filter((entry) => entry.model_id !== `${stem}-model`)
    .map((entry) => ({
      model_id: entry.model_id,
      agent_configuration_revision_hash: entry.agent_configuration_revision_hash
    }));
  const registry = await page.request.put(`/atelier/api/v1/model-registries/${providerId}`, {
    data: {
      revision_number: (currentRegistry?.revision_number ?? 0) + 1,
      entries: [
        ...keptEntries,
        { model_id: `${stem}-model`, agent_configuration_revision_hash: configurationHash }
      ]
    }
  });
  expect([200, 201]).toContain(registry.status());
  const checked = await page.request.post(`/atelier/api/v1/model-registries/${providerId}/validations`, {
    data: { agent_configuration_revision_hash: configurationHash }
  });
  expect([200, 201]).toContain(checked.status());
  return configurationHash;
}

async function admitWorkflow(page: Page, name: string, schemaHash: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: workflowDocument(name, schemaHash)
  });
  expect([200, 201]).toContain(published.status());
  const workflowRevisionHash = (await published.json()).workflow_revision_hash as string;
  const admitted = await page.request.post("/atelier/api/v1/catalog-lineages", {
    data: {
      kind: "workflow",
      catalog_revision_hash: workflowRevisionHash,
      actor: "retire-e2e",
      activated_at: "2026-08-29T00:00:00Z"
    }
  });
  expect([200, 201]).toContain(admitted.status());
  return workflowRevisionHash;
}

async function completeRun(page: Page, name: string, revisionHash: string, configurationHash: string): Promise<string> {
  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: `retire-e2e/${name}`,
      workflow_revision_hash: revisionHash,
      agent_bindings: [{ role: "builder", agent_configuration_revision_hash: configurationHash }],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;
  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 20_000 });
  return reference;
}

function catalogEntry(page: Page, name: string) {
  return page.getByRole("listitem").filter({ hasText: name });
}

function scenarioName(stem: string, testInfo: TestInfo): string {
  return `${stem}-${testInfo.repeatEachIndex}-${testInfo.retry}-${Date.now()}`;
}

async function openTechnicalFold(page: Page, name: string): Promise<void> {
  await page.getByRole("link", { name }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await page.locator("summary", { hasText: workflowDetailCopy.technical }).click();
}

async function retireFromTechnical(page: Page, name: string): Promise<void> {
  await openTechnicalFold(page, name);
  await page.getByRole("button", { name: workflowDetailCopy.retire }).click();
  const sheet = page.getByRole("dialog", { name: workflowDetailCopy.retireTitle(name) });
  await expect(sheet.getByText(workflowDetailCopy.retireDisappearsFact)).toBeVisible();
  await expect(sheet.getByText(workflowDetailCopy.retireStaysFact)).toBeVisible();
  await expect(sheet.getByText(workflowDetailCopy.retirePermanentFact)).toBeVisible();
  await expect(sheet.getByRole("button", { name: /again/i })).toHaveCount(0);
  await sheet.getByRole("button", { name: workflowDetailCopy.retire }).click();
}

test("proves(a-catalog-entry-can-be-taken-off-the-shelf): retiring a workflow leaves a completed run and immutable revision reachable", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const name = scenarioName("retire-shelf", testInfo);
  const schemaHash = await anyJsonSchema(page);
  const configurationHash = await checkedAgentConfiguration(page, name);
  const revisionHash = await admitWorkflow(page, name, schemaHash);
  const runReference = await completeRun(page, name, revisionHash, configurationHash);

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/atelier/catalog");
  await expect(catalogEntry(page, name)).toBeVisible();
  await retireFromTechnical(page, name);
  await expect(page).toHaveURL(/\/atelier\/catalog$/);
  await expect(catalogEntry(page, name)).toHaveCount(0);

  await page.goto(`/atelier/catalog/${name}`);
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await expect(page.getByRole("button", { name: catalogPageCopy.start })).toHaveCount(0);

  await page.goto("/atelier/history");
  const historyRow = page.getByRole("link", { name: new RegExp(name) });
  await expect(historyRow).toBeVisible();
  await historyRow.click();
  await expect(page).toHaveURL(new RegExp(`/atelier/runs/${runReference}`));
  await expect(page.getByText(name, { exact: true })).toBeVisible();
});

test("proves(a-workflow-detail-names-its-revision-and-format-in-the-technical-fold): a catalog revision's Technical fold shows revision and format, no source row for a file-imported revision", async ({ page }, testInfo) => {
  const name = scenarioName("technical-fold", testInfo);
  const schemaHash = await anyJsonSchema(page);
  await admitWorkflow(page, name, schemaHash);

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/atelier/catalog");
  await expect(catalogEntry(page, name)).toBeVisible();
  await openTechnicalFold(page, name);

  await expect(page.getByRole("group", { name: workflowDetailCopy.workflowRevision })).toBeVisible();
  await expect(page.getByText(workflowDetailCopy.format)).toBeVisible();
  await expect(page.getByText(workflowDetailCopy.formatVersion(3))).toBeVisible();
  await expect(page.getByText(workflowDetailCopy.source)).toHaveCount(0);
});

test("proves(an-unwanted-catalog-addition-can-be-taken-back): a workflow added by mistake can leave the shelf", async ({ page }, testInfo) => {
  const name = scenarioName("retire-import", testInfo);
  const schemaHash = await anyJsonSchema(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/catalog");
  await page.getByLabel(catalogPageCopy.filePicker).setInputFiles({
    name: `${name}.yaml`,
    mimeType: "application/yaml",
    buffer: Buffer.from(workflowDocument(name, schemaHash))
  });
  const importSheet = page.getByRole("dialog", { name: catalogPageCopy.import });
  await importSheet.getByRole("button", { name: catalogPageCopy.kindWorkflow, exact: true }).click();
  await importSheet.getByRole("button", { name: catalogPageCopy.addToCatalog }).click();
  await expect(catalogEntry(page, name)).toBeVisible();

  await retireFromTechnical(page, name);
  await expect(catalogEntry(page, name)).toHaveCount(0);
});
