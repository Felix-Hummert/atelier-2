import { expect, test, type Page } from "@playwright/test";

import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { historyWhenLabel } from "../../src/lib/historyRows";
import { standingWords } from "../../src/lib/runState";

const VIEWPORTS = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;

const RESULT_TEXT = "V3 provider bytes";

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

async function publishCheckedModelRegistry(
  page: Page,
  providerId: string,
  modelId: string,
  configurationHash: string
): Promise<void> {
  const endpoint = `/atelier/api/v1/model-registries/${encodeURIComponent(providerId)}`;
  const current = await page.request.get(endpoint);
  expect([200, 404]).toContain(current.status());
  const registry = current.status() === 200
    ? await current.json() as {
      revision_number: number;
      entries: Array<{
        model_id: string;
        agent_configuration_revision_hash: string;
        source: string;
        provider_check: string;
      }>;
    }
    : undefined;
  const entriesByModelId = new Map(registry?.entries.map((entry) => [entry.model_id, entry]));
  entriesByModelId.set(modelId, {
    model_id: modelId,
    agent_configuration_revision_hash: configurationHash,
    source: "operator",
    provider_check: "checked"
  });
  const published = await page.request.put(endpoint, {
    data: {
      revision_number: registry === undefined ? 1 : registry.revision_number + 1,
      entries: [...entriesByModelId.values()].map((entry) => ({
        model_id: entry.model_id,
        agent_configuration_revision_hash: entry.agent_configuration_revision_hash
      }))
    }
  });
  expect([200, 201]).toContain(published.status());
  const registryBody = await published.json() as {
    entries: Array<{ agent_configuration_revision_hash: string; provider_check: string }>;
  };
  if (registryBody.entries.find(
    (entry) => entry.agent_configuration_revision_hash === configurationHash
  )?.provider_check !== "checked") {
    const validation = await page.request.post(`${endpoint}/validations`, {
      data: { agent_configuration_revision_hash: configurationHash }
    });
    expect([200, 201]).toContain(validation.status());
  }
}

async function publishWorkflow(page: Page, name: string, schemaHash: string): Promise<string> {
  const yaml = [
    "format_version: 3",
    `name: ${name}`,
    "nodes:",
    "  - id: build",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing.",
    "    outputs:",
    "      - name: result",
    "        schema:",
    "          ref: result-schema",
    `          revision: ${schemaHash}`,
    ""
  ].join("\n");
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: yaml
  });
  expect(published.status()).toBe(201);
  return (await published.json()).workflow_revision_hash as string;
}

async function startCompletedRun(
  page: Page,
  runId: string,
  revisionHash: string,
  agentHash: string
): Promise<{ reference: string; endedAt: string }> {
  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [{ role: "builder", agent_configuration_revision_hash: agentHash }],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;
  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 20_000 });
  const finished = await page.request.get(`/atelier/api/v1/runs/${reference}`);
  const endedAt = (await finished.json()).ended_at as string;
  expect(endedAt).toBeTruthy();
  return { reference, endedAt };
}

test("proves(a-history-row-names-when-work-and-result): two finished runs of the same workflow name when they ran, a work-item dash, and the result sentence", async ({
  page
}, testInfo) => {
  test.setTimeout(120_000);
  const token = `${testInfo.repeatEachIndex}-${testInfo.retry}`;
  const workflowName = `history-717-same-recipe-${token}`;
  const schemaHash = await anyJsonSchema(page);

  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: `history-717-${token}`,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: `history-717-model-${token}`,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedModelRegistry(page, "e2e-v3", `history-717-model-${token}`, agentHash);

  const revisionHash = await publishWorkflow(page, workflowName, schemaHash);
  const first = await startCompletedRun(page, `history-717/first-${token}`, revisionHash, agentHash);
  const firstClock = historyWhenLabel(first.endedAt, new Date()).clock;
  await expect(async () => {
    expect(historyWhenLabel(new Date().toISOString(), new Date()).clock).not.toBe(firstClock);
  }).toPass({ timeout: 2_500 });
  await startCompletedRun(page, `history-717/second-${token}`, revisionHash, agentHash);

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier/history");
    const rows = page.getByRole("link", { name: new RegExp(workflowName) });
    await expect(rows).toHaveCount(2);
    for (const row of await rows.all()) {
      await expect(row.locator(".row-result")).toContainText(RESULT_TEXT);
      await expect(row.locator(".row-result")).not.toHaveText(standingWords.done);
      await expect(row.locator(".row-when")).toBeVisible();
      if (viewport.width === 1280) {
        await expect(row.locator(".row-work-item")).toContainText(historyPageCopy.workItemPlaceholder);
      }
    }
    const firstText = await rows.nth(0).innerText();
    const secondText = await rows.nth(1).innerText();
    expect(firstText).not.toBe(secondText);
    const firstName = await rows.nth(0).evaluate((element) => {
      const host = element as HTMLElement;
      return host.getAttribute("aria-label") ?? host.innerText;
    });
    const secondName = await rows.nth(1).evaluate((element) => {
      const host = element as HTMLElement;
      return host.getAttribute("aria-label") ?? host.innerText;
    });
    expect(firstName).not.toBe(secondName);

    if (viewport.width === 390) {
      const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      const viewportWidth = await page.evaluate(() => document.documentElement.clientWidth);
      expect(documentWidth).toBeLessThanOrEqual(viewportWidth);
    }
  }
});
