import { mkdirSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

const shotDirectory = process.env.ATELIER2_SHOT_DIR ?? "";

const workflowName = "catalog-detail-shot";
const viewports = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;
const colorSchemes = ["light", "dark"] as const;

const workItemSchemaDocument =
  '{"$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"body":{"type":"string"},"change_marker":{"maxLength":1024,"minLength":1,"type":"string"},"digest":{"pattern":"^[0-9a-f]{64}$","type":"string"},"kind":{"enum":["issue","change_request"],"type":"string"},"observed_at":{"pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$","type":"string"},"reference":{"maxLength":1024,"minLength":1,"type":"string"}},"required":["body","change_marker","digest","kind","observed_at","reference"],"title":"work item","type":"object"}';

async function publishCheckedRegistryEntry(
  page: Page,
  providerId: string,
  modelId: string,
  configurationHash: string
): Promise<void> {
  const current = await page.request.get(`/atelier/api/v1/model-registries/${providerId}`);
  const currentRegistry = current.status() === 200
    ? await current.json() as {
        revision_number: number;
        entries: Array<{ model_id: string; agent_configuration_revision_hash: string }>;
      }
    : null;
  if (currentRegistry === null) expect(current.status()).toBe(404);
  const existingEntries = (currentRegistry?.entries ?? [])
    .filter((entry) => entry.agent_configuration_revision_hash !== configurationHash)
    .map((entry) => ({
      model_id: entry.model_id,
      agent_configuration_revision_hash: entry.agent_configuration_revision_hash
    }));
  const registry = await page.request.put(`/atelier/api/v1/model-registries/${providerId}`, {
    data: {
      revision_number: (currentRegistry?.revision_number ?? 0) + 1,
      entries: [...existingEntries, {
        model_id: modelId,
        agent_configuration_revision_hash: configurationHash
      }]
    }
  });
  expect([200, 201]).toContain(registry.status());
  const checked = await page.request.post(`/atelier/api/v1/model-registries/${providerId}/validations`, {
    data: { agent_configuration_revision_hash: configurationHash }
  });
  expect([200, 201]).toContain(checked.status());
}

async function publishStartableConfiguration(page: Page, repetition: number): Promise<{
  agentConfigurationRevisionHash: string;
  authProfileRevisionHash: string;
}> {
  const profileId = `start-sheet-e2e-${repetition}`;
  const modelId = `start-sheet-model-${repetition}`;
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: profileId,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const authProfileRevisionHash = (await auth.json()).auth_profile_revision_hash as string;
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: modelId,
      auth_profile_revision_hash: authProfileRevisionHash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  const agentConfigurationRevisionHash =
    (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3",
    modelId,
    agentConfigurationRevisionHash
  );
  return {
    agentConfigurationRevisionHash,
    authProfileRevisionHash
  };
}

test("captures the Catalog list, detail, and start sheet at both requested widths", async ({ page }) => {
  test.skip(shotDirectory === "", "no shot directory named");
  mkdirSync(shotDirectory, { recursive: true });
  const workItemSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: workItemSchemaDocument
  });
  expect(workItemSchema.status()).toBe(201);
  const workItemSchemaHash = (await workItemSchema.json()).schema_revision_hash as string;
  const schema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect(schema.status()).toBe(201);
  const schemaHash = (await schema.json()).schema_revision_hash as string;

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: "catalog-shots",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "catalog-shot-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  await publishCheckedRegistryEntry(
    page,
    "e2e-v3",
    "catalog-shot-model",
    (await configuration.json()).agent_configuration_revision_hash as string
  );
  const revision = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "graph_inputs:",
      "  - name: work_item",
      "    schema:",
      "      ref: work-item",
      `      revision: ${workItemSchemaHash}`,
      "nodes:",
      "  - id: build",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Build the catalog detail.",
      "    inputs:",
      "      - name: work_item",
      "        from:",
      "          graph_input: work_item",
      `    outputs: [{name: result, schema: {ref: any, revision: ${schemaHash}}}]`,
      ""
    ].join("\n")
  });
  expect(revision.status()).toBe(201);
  const revisionHash = (await revision.json()).workflow_revision_hash as string;
  const lineage = await page.request.post("/atelier/api/v1/workflow-lineages", {
    data: {
      workflow_revision_hash: revisionHash,
      actor: "catalog-shots",
      activated_at: "2026-08-26T00:00:00Z"
    }
  });
  expect(lineage.status()).toBe(201);
  const newerRevision = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "description: A newer published revision that the Catalog marks.",
      "graph_inputs:",
      "  - name: work_item",
      "    schema:",
      "      ref: work-item",
      `      revision: ${workItemSchemaHash}`,
      "nodes:",
      "  - id: build",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Build the catalog detail.",
      "    inputs:",
      "      - name: work_item",
      "        from:",
      "          graph_input: work_item",
      `    outputs: [{name: result, schema: {ref: any, revision: ${schemaHash}}}]`,
      ""
    ].join("\n")
  });
  expect(newerRevision.status()).toBe(201);

  let observedItems = true;
  await page.route("**/atelier/api/v1/observed-queue-items*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: observedItems ? [{
          project_id: "atelier-2",
          tracker_item_reference: "gh:450",
          item_id: "f".repeat(64),
          revision: 0
        }] : [],
        next_after: null
      })
    });
  });

  for (const colorScheme of colorSchemes) {
    await page.emulateMedia({ colorScheme });
    for (const viewport of viewports) {
      observedItems = viewport.name === "1280";
      await page.setViewportSize(viewport);
      await page.goto("/atelier/catalog");
      await expect(page.getByRole("heading", { name: "Catalog" })).toBeVisible();
      const markedWorkflow = page.getByRole("listitem").filter({ hasText: workflowName });
      await expect(markedWorkflow).toBeVisible();
      const whyMarked = markedWorkflow.getByRole("button", { name: "Why this card is marked" });
      await expect(whyMarked).toBeVisible();
      await page.screenshot({ path: `${shotDirectory}/catalog-list-${viewport.name}-${colorScheme}.png`, fullPage: true });

      if (viewport.name === "390") {
        await whyMarked.click();
        const popover = markedWorkflow.getByRole("status");
        await expect(popover).toBeVisible();
        await page.screenshot({ path: `${shotDirectory}/catalog-list-why-390-${colorScheme}.png`, fullPage: true });
      }

      const workflowEntry = page.getByRole("listitem").filter({ hasText: workflowName });
      await workflowEntry.getByRole("link", { name: "Details" }).click();
      await expect(page.getByRole("heading", { name: workflowName })).toBeVisible();
      await page.screenshot({ path: `${shotDirectory}/catalog-detail-${viewport.name}-${colorScheme}.png`, fullPage: true });

      await page.getByRole("button", { name: "Start" }).click();
      const sheet = page.getByRole("dialog", { name: `Start ${workflowName}` });
      await expect(sheet).toBeVisible();
      await expect(page.getByLabel("Configuration for builder")).toBeVisible();
      await expect(sheet.getByText(workItemSchemaHash)).toHaveCount(0);
      if (observedItems) {
        await page.getByLabel("Work item for work_item").selectOption("gh:450");
      } else {
        await expect(page.getByRole("button", { name: "Settings" })).toBeVisible();
      }
      await page.screenshot({ path: `${shotDirectory}/catalog-start-sheet-${viewport.name}-${colorScheme}.png`, fullPage: true });
    }
  }
});

test("proves(a-v3-workflow-is-started-from-the-picker): starts an admitted Catalog workflow with its observed work item and a checked role configuration", async ({ page }, testInfo) => {
  const repetition = testInfo.repeatEachIndex;
  const workflowName = `start-sheet-work-item-e2e-${repetition}`;
  const profileId = `start-sheet-e2e-${repetition}`;
  const modelId = `start-sheet-model-${repetition}`;
  const workItemSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: workItemSchemaDocument
  });
  expect([200, 201]).toContain(workItemSchema.status());
  const workItemSchemaHash = (await workItemSchema.json()).schema_revision_hash as string;
  const configuration = await publishStartableConfiguration(page, repetition);
  const outputSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(outputSchema.status());
  const outputSchemaHash = (await outputSchema.json()).schema_revision_hash as string;
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "graph_inputs:",
      "  - name: work_item",
      "    schema:",
      "      ref: work-item",
      `      revision: ${workItemSchemaHash}`,
      "nodes:",
      "  - id: build",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Build the selected work item.",
      "    inputs:",
      "      - name: work_item",
      "        from:",
      "          graph_input: work_item",
      "    outputs:",
      "      - name: result",
      "        schema:",
      "          ref: result-schema",
      `          revision: ${outputSchemaHash}`,
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const workflowRevisionHash = (await published.json()).workflow_revision_hash as string;
  const admitted = await page.request.post("/atelier/api/v1/workflow-lineages", {
    data: {
      workflow_revision_hash: workflowRevisionHash,
      actor: profileId,
      activated_at: "2026-08-26T00:00:00Z"
    }
  });
  expect(admitted.status()).toBe(201);

  await page.route("**/atelier/api/v1/observed-queue-items*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{
          project_id: "atelier-2",
          tracker_item_reference: "gh:450",
          item_id: "f".repeat(64),
          revision: 0
        }],
        next_after: null
      })
    });
  });

  let receivedStart: Record<string, unknown> | null = null;
  let startedRun: Record<string, unknown> | null = null;
  await page.route("**/atelier/api/v1/runs", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    receivedStart = route.request().postDataJSON() as Record<string, unknown>;
    const runId = receivedStart.run_id as string;
    const publicRunReference = `run1.${Buffer.from(runId).toString("base64url")}`;
    const hash = "a".repeat(64);
    startedRun = {
      workflow_format_version: 3,
      run_id: runId,
      public_run_reference: publicRunReference,
      workflow_revision_hash: workflowRevisionHash,
      agent_binding_set_hash: hash,
      run_configuration_revision_hash: hash,
      agent_bindings: [{
        role: "builder",
        agent_configuration_revision_hash: configuration.agentConfigurationRevisionHash,
        auth_profile_revision_hash: configuration.authProfileRevisionHash,
        profile_id: profileId,
        revision_number: 1,
        provider_id: "e2e-v3",
        auth_mode: "subscription",
        model: modelId,
        executor_revision: "immediate/v1"
      }],
      orders: [{ name: "work_item", bytes: 0, schema_revision_hash: workItemSchemaHash }],
      state_version: 0,
      state: "STARTED",
      current_node_id: "build",
      node_rail: [{ node_id: "build", state: "queued", attempt: null }],
      cancellation: { cancellable: false, reason: "between-nodes", target_node_execution_id: null },
      terminal_hash: null,
      latest_event_cursor: null
    };
    await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(startedRun) });
  });
  await page.route("**/atelier/api/v1/runs/*", async (route) => {
    if (route.request().method() === "GET" && startedRun !== null) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(startedRun) });
      return;
    }
    await route.continue();
  });

  await page.goto(`/atelier/catalog/${workflowName}`);
  const opener = page.getByRole("button", { name: "Start" });
  await opener.click();
  const sheet = page.getByRole("dialog", { name: `Start ${workflowName}` });
  await expect(sheet).toBeVisible();
  const workItem = sheet.getByLabel("Work item for work_item");
  await expect(sheet.getByRole("button", { name: "Cancel" })).toBeFocused();
  await expect(workItem).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(opener).toBeFocused();
  await opener.click();
  await expect(sheet).toBeVisible();
  await workItem.selectOption({ label: "GitHub · gh:450" });
  const roleConfiguration = sheet.getByLabel("Configuration for builder");
  await roleConfiguration.selectOption(configuration.agentConfigurationRevisionHash);
  await expect(sheet.getByText("Chosen now", { exact: true })).toBeVisible();

  const startRun = sheet.getByRole("button", { name: "Start run" });
  await expect(startRun).toBeEnabled();
  await startRun.click();
  await expect.poll(() => receivedStart).not.toBeNull();
  expect(receivedStart).toEqual(expect.objectContaining({
    workflow_format_version: 3,
    workflow_revision_hash: workflowRevisionHash,
    agent_bindings: [{ role: "builder", agent_configuration_revision_hash: configuration.agentConfigurationRevisionHash }],
    orders: [{ name: "work_item", work_item: "gh:450" }]
  }));
  await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
  await expect(page.getByRole("heading", { name: workflowName })).toBeVisible();
});
