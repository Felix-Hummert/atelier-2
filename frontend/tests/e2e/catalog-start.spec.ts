import { createHash } from "node:crypto";

import { expect, test, type Locator, type Page } from "@playwright/test";
import { z } from "zod";

import { nodeDetailSchema } from "../../src/api/client";
import { observedWorkItemLabel, workflowStartCopy } from "../../src/lib/catalogPageCopy";
import { decodeUtf8Base64 } from "../../src/lib/exactBytes";
import { WORK_ITEM_ORDER_SCHEMA_REVISION } from "../../src/lib/orderSchema";

const VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 1280, height: 900 }
] as const;

const workItemSchemaDocument =
  '{"$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"body":{"type":"string"},"change_marker":{"maxLength":1024,"minLength":1,"type":"string"},"digest":{"pattern":"^[0-9a-f]{64}$","type":"string"},"kind":{"enum":["issue","change_request"],"type":"string"},"observed_at":{"pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$","type":"string"},"reference":{"maxLength":1024,"minLength":1,"type":"string"}},"required":["body","change_marker","digest","kind","observed_at","reference"],"title":"work item","type":"object"}';
const observedWorkItemBody = "e2e observed work item gh:450 — Grüße 東京";
const workItemPickerName = `${workflowStartCopy.workItem} for work_item`;
// The tracker title the harness seeds for gh:450 (tests/e2e/serve_cockpit.py
// _E2E_TRACKER_ITEM_TITLE) -- the picker option now carries it beside the
// reference (#1030).
const observedWorkItemTitle = "e2e observed work item gh:450";
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

test("proves(a-v3-workflow-is-started-from-the-picker): starts an admitted Catalog workflow with its observed work item at 390 and 1280", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    const token = `${viewport.width}-${testInfo.repeatEachIndex}`;
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
    const sheet = page.getByRole("dialog", { name: workflowStartCopy.startTitle(workflowName) });
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
    await pickWorkItem(sheet, observedWorkItemLabel("#450", observedWorkItemTitle));
    const roleConfiguration = sheet.getByLabel(workflowStartCopy.configurationFor("builder"));
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
  }
});
