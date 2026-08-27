import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";

import { expect, test, type Locator, type Page } from "@playwright/test";
import { z } from "zod";

import { nodeDetailSchema } from "../../src/api/client";
import { workflowStartCopy } from "../../src/lib/catalogPageCopy";
import { decodeUtf8Base64 } from "../../src/lib/exactBytes";
import { WORK_ITEM_ORDER_SCHEMA_REVISION } from "../../src/lib/orderSchema";

const shotDirectory = process.env.ATELIER2_SHOT_DIR ?? "";

const workflowName = "catalog-detail-shot";
const viewports = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;
const colorSchemes = ["light", "dark"] as const;

const workItemSchemaDocument =
  '{"$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"body":{"type":"string"},"change_marker":{"maxLength":1024,"minLength":1,"type":"string"},"digest":{"pattern":"^[0-9a-f]{64}$","type":"string"},"kind":{"enum":["issue","change_request"],"type":"string"},"observed_at":{"pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$","type":"string"},"reference":{"maxLength":1024,"minLength":1,"type":"string"}},"required":["body","change_marker","digest","kind","observed_at","reference"],"title":"work item","type":"object"}';
const observedWorkItemBody = "e2e observed work item gh:450 — Grüße 東京";
const workItemPickerName = `${workflowStartCopy.workItem} for work_item`;
const observedWorkItemRevision = {
  body: observedWorkItemBody,
  change_marker: "e2e-etag-gh-450",
  digest: createHash("sha256").update(observedWorkItemBody, "utf8").digest("hex"),
  kind: "issue",
  observed_at: "2026-08-26T09:15:00Z",
  reference: "gh:450"
} as const;
const workItemOrderValueSchema = z
  .object({
    body: z.string(),
    change_marker: z.string().min(1),
    digest: z.string().regex(/^[0-9a-f]{64}$/),
    kind: z.enum(["issue", "change_request"]),
    observed_at: z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/),
    reference: z.string().min(1)
  })
  .strict();
const groupedObservedQueueItems = {
  items: [
    {
      project_id: "atelier-2",
      tracker_item_reference: "gh:450",
      item_id: "a".repeat(64),
      revision: 0
    },
    {
      project_id: "atelier-2",
      tracker_item_reference: "gh:446",
      item_id: "b".repeat(64),
      revision: 0
    },
    {
      project_id: "infra",
      tracker_item_reference: "gl:12",
      item_id: "c".repeat(64),
      revision: 0
    }
  ],
  next_after: null
};

async function pickWorkItem(sheet: Locator, optionLabel: string): Promise<void> {
  const picker = sheet.getByRole("combobox", { name: workItemPickerName });
  await picker.focus();
  await picker.press("ArrowDown");
  const listbox = sheet.getByRole("listbox", { name: workItemPickerName });
  await expect(listbox).toBeVisible();
  const option = sheet.getByRole("option", { name: optionLabel, exact: true });
  await expect(option).toBeVisible();
  const optionId = await option.getAttribute("id");
  expect(optionId).toBeTruthy();
  while ((await picker.getAttribute("aria-activedescendant")) !== optionId) {
    const before = await picker.getAttribute("aria-activedescendant");
    await picker.press("ArrowDown");
    expect(await picker.getAttribute("aria-activedescendant")).not.toBe(before);
  }
  await picker.press("Enter");
  await expect(listbox).toHaveCount(0);
  await expect(picker).toContainText(optionLabel);
}

async function openGroupedWorkItemPicker(sheet: Locator): Promise<void> {
  const picker = sheet.getByRole("combobox", { name: workItemPickerName });
  await picker.click();
  await expect(sheet.getByRole("listbox", { name: workItemPickerName })).toBeVisible();
  await expect(sheet.getByText("atelier-2 · GitHub")).toBeVisible();
  await expect(sheet.getByText("infra · GitLab")).toBeVisible();
  await expect(sheet.getByRole("option", { name: "#450", exact: true })).toBeVisible();
  await expect(sheet.getByRole("option", { name: "#446", exact: true })).toBeVisible();
  await expect(sheet.getByRole("option", { name: "!12", exact: true })).toBeVisible();
}

function observedWorkItemRevisionFromJob(job: string): z.infer<typeof workItemOrderValueSchema> {
  for (const block of job.split("\n\n")) {
    if (!block.startsWith("{")) continue;
    try {
      return workItemOrderValueSchema.parse(JSON.parse(block));
    } catch {
      continue;
    }
  }
  throw new Error("the node job did not carry an observed work item revision");
}

async function readObservedWorkItemRevision(
  page: Page,
  publicRef: string,
  nodeId: string
): Promise<z.infer<typeof workItemOrderValueSchema> | null> {
  const response = await page.request.get(`/atelier/api/v1/runs/${publicRef}/nodes/${nodeId}`);
  if (!response.ok()) return null;
  const detail = nodeDetailSchema.parse(await response.json());
  if (detail.node_id !== nodeId) {
    throw new Error("The node response named another node.");
  }
  if (detail.job_base64 === null || detail.job_base64.length === 0) return null;
  const job = decodeUtf8Base64(detail.job_base64);
  if (job === null) throw new Error("The node job was not UTF-8.");
  return observedWorkItemRevisionFromJob(job);
}

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

async function publishStartableConfiguration(page: Page, token: string): Promise<{
  agentConfigurationRevisionHash: string;
  authProfileRevisionHash: string;
}> {
  const profileId = `start-sheet-e2e-${token}`;
  const modelId = `start-sheet-model-${token}`;
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
  expect([200, 201]).toContain(workItemSchema.status());
  const workItemSchemaHash = (await workItemSchema.json()).schema_revision_hash as string;
  const schema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(schema.status());
  const schemaHash = (await schema.json()).schema_revision_hash as string;

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: "catalog-shots",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "catalog-shot-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
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
  expect([200, 201]).toContain(revision.status());
  const revisionHash = (await revision.json()).workflow_revision_hash as string;
  const lineage = await page.request.post("/atelier/api/v1/workflow-lineages", {
    data: {
      workflow_revision_hash: revisionHash,
      actor: "catalog-shots",
      activated_at: "2026-08-26T00:00:00Z"
    }
  });
  expect([200, 201]).toContain(lineage.status());
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
  expect([200, 201]).toContain(newerRevision.status());

  await page.route("**/atelier/api/v1/observed-queue-items*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(groupedObservedQueueItems)
    });
  });

  for (const colorScheme of colorSchemes) {
    await page.emulateMedia({ colorScheme });
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      await page.goto("/atelier/catalog");
      await expect(page.getByRole("heading", { name: "Catalog" })).toBeVisible();
      const markedWorkflow = page.getByRole("listitem").filter({ hasText: workflowName });
      await expect(markedWorkflow).toBeVisible();
      await expect(markedWorkflow.getByText("Newer revision")).toBeVisible();
      await page.screenshot({ path: `${shotDirectory}/catalog-list-${viewport.name}-${colorScheme}.png`, fullPage: true });

      await page.locator(".catalog-drop-target").dispatchEvent("dragover");
      await expect(page.getByText("Drop a workflow, an agent, or a plugin folder — anywhere on this page.")).toBeVisible();
      await page.screenshot({ path: `${shotDirectory}/catalog-import-dragover-${viewport.name}-${colorScheme}.png`, fullPage: true });
      await page.locator(".catalog-drop-target").dispatchEvent("dragleave");

      await page.getByLabel("Catalog file picker").setInputFiles({
        name: "catalog-recognition-shot.yaml",
        mimeType: "application/yaml",
        buffer: Buffer.from([
          "format_version: 3",
          "name: catalog-recognition-shot",
          "nodes:",
          "  - id: inspect",
          "    type: wait",
          "    prompt: Is this import ready?",
          "    outputs:",
          "      - name: answer",
          "        schema:",
          "          ref: answer-schema",
          `          revision: ${schemaHash}`,
          ""
        ].join("\n"))
      });
      const importSheet = page.getByRole("dialog", { name: "Import" });
      await expect(importSheet).toBeVisible();
      await expect(importSheet.getByText("⧉")).toBeVisible();
      if (viewport.name === "390") {
        await expect(importSheet.getByText("1 workflow")).toBeHidden();
      } else {
        await expect(importSheet.getByText("1 workflow")).toBeVisible();
      }
      await expect(importSheet.getByText("catalog-recognition-shot")).toBeVisible();
      await expect(importSheet.getByRole("button", { name: "Add to catalog" })).toBeFocused();
      await page.screenshot({ path: `${shotDirectory}/catalog-import-found-${viewport.name}-${colorScheme}.png`, fullPage: true });
      await importSheet.getByRole("button", { name: "Cancel" }).click();

      const workflowEntry = page.getByRole("listitem").filter({ hasText: workflowName });
      await workflowEntry.getByRole("link", { name: workflowName }).click();
      await expect(page.getByRole("heading", { name: workflowName })).toBeVisible();
      await expect(page.getByRole("group", { name: "Workflow revision" })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Copy Workflow revision" })).toHaveCount(0);
      await page.screenshot({ path: `${shotDirectory}/catalog-detail-${viewport.name}-${colorScheme}.png`, fullPage: true });

      await page.locator("summary", { hasText: "Technical" }).click();
      await expect(page.getByRole("group", { name: "Workflow revision" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Copy Workflow revision" })).toBeVisible();

      await page.getByRole("button", { name: "Start" }).click();
      const sheet = page.getByRole("dialog", { name: `Start ${workflowName}` });
      await expect(sheet).toBeVisible();
      await expect(page.getByLabel("Configuration for builder")).toBeVisible();
      await expect(sheet.getByText(workItemSchemaHash)).toHaveCount(0);
      await openGroupedWorkItemPicker(sheet);
      await page.screenshot({ path: `${shotDirectory}/catalog-start-sheet-${viewport.name}-${colorScheme}.png`, fullPage: true });
    }
  }
});

for (const viewport of viewports) {
  test(`proves(a-v3-workflow-is-started-from-the-picker): starts an admitted Catalog workflow with its observed work item at ${viewport.name}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const token = `${viewport.name}-${testInfo.repeatEachIndex}`;
    const workflowName = `start-sheet-work-item-e2e-${token}`;
    const profileId = `start-sheet-e2e-${token}`;
    const workItemSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
      headers: { "content-type": "application/json" },
      data: workItemSchemaDocument
    });
    expect([200, 201]).toContain(workItemSchema.status());
    const workItemSchemaHash = (await workItemSchema.json()).schema_revision_hash as string;
    expect(workItemSchemaHash).toBe(WORK_ITEM_ORDER_SCHEMA_REVISION);
    const configuration = await publishStartableConfiguration(page, token);
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

    await page.goto(`/atelier/catalog/${workflowName}`);
    const opener = page.getByRole("button", { name: "Start" });
    await opener.click();
    const sheet = page.getByRole("dialog", { name: `Start ${workflowName}` });
    await expect(sheet).toBeVisible();
    const workItem = sheet.getByRole("combobox", { name: workItemPickerName });
    await expect(sheet.getByRole("button", { name: workflowStartCopy.cancel })).toBeFocused();
    await expect(workItem).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);
    await expect(opener).toBeFocused();
    await opener.click();
    await expect(sheet).toBeVisible();
    await workItem.focus();
    await page.keyboard.press("ArrowDown");
    await expect(sheet.getByRole("listbox", { name: workItemPickerName })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet.getByRole("listbox", { name: workItemPickerName })).toHaveCount(0);
    await expect(sheet).toBeVisible();
    await expect(workItem).toBeFocused();
    await pickWorkItem(sheet, "#450");
    const roleConfiguration = sheet.getByLabel("Configuration for builder");
    await roleConfiguration.selectOption(configuration.agentConfigurationRevisionHash);
    await expect(sheet.getByText("Chosen now", { exact: true })).toBeVisible();

    const startRun = sheet.getByRole("button", { name: workflowStartCopy.startRun });
    await expect(startRun).toBeEnabled();
    await startRun.click();
    await expect(page).toHaveURL(/\/atelier\/runs\//);
    await expect(page.getByRole("heading", { name: workflowName })).toBeVisible();
    const publicRef = new URL(page.url()).pathname.split("/").at(-1);
    expect(publicRef).toBeTruthy();
    await expect.poll(async () => {
      return await readObservedWorkItemRevision(page, publicRef!, "build");
    }, { timeout: 15_000 }).toEqual(observedWorkItemRevision);
  });
}
