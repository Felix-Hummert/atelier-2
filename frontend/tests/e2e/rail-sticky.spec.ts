import { expect, test, type Page } from "@playwright/test";

import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { WORKSHOP_DESTINATION } from "../../src/lib/workshop";

const API = "/atelier/api/v1";
const VIEWPORTS = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;
const COMPLETED_RUN_COUNT = 20;

type OverflowScroller = {
  kind: "stage" | "document";
  scrollHeight: number;
  clientHeight: number;
};

async function publishSchema(page: Page): Promise<string> {
  const published = await page.request.post(`${API}/schema-revisions`, {
    headers: { "content-type": "application/json" },
    data: "true"
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

async function publishCheckedRegistryEntry(
  page: Page,
  providerId: string,
  modelId: string,
  configurationHash: string
): Promise<void> {
  const current = await page.request.get(`${API}/model-registries/${providerId}`);
  const currentRegistry = current.status() === 200
    ? await current.json() as {
      revision_number: number;
      entries: Array<{ model_id: string; agent_configuration_revision_hash: string }>;
    }
    : null;
  if (currentRegistry === null) expect(current.status()).toBe(404);
  const existingEntries = (currentRegistry?.entries ?? [])
    .filter((entry) =>
      entry.agent_configuration_revision_hash !== configurationHash && entry.model_id !== modelId
    )
    .map((entry) => ({
      model_id: entry.model_id,
      agent_configuration_revision_hash: entry.agent_configuration_revision_hash
    }));
  const registry = await page.request.put(`${API}/model-registries/${providerId}`, {
    data: {
      revision_number: (currentRegistry?.revision_number ?? 0) + 1,
      entries: [
        ...existingEntries,
        { model_id: modelId, agent_configuration_revision_hash: configurationHash }
      ]
    }
  });
  expect([200, 201]).toContain(registry.status());
  const checked = await page.request.post(`${API}/model-registries/${providerId}/validations`, {
    data: { agent_configuration_revision_hash: configurationHash }
  });
  expect([200, 201]).toContain(checked.status());
}

async function seedOverflowingHistory(page: Page, suffix: number): Promise<string> {
  const workflowName = `rail-sticky-history-${suffix}`;
  const schemaHash = await publishSchema(page);
  const published = await page.request.post(`${API}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "nodes:",
      "  - id: work",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Finish so History overflows the stage.",
      "    outputs:",
      "      - name: result",
      "        schema:",
      "          ref: result-schema",
      `          revision: ${schemaHash}`,
      ""
    ].join("\n")
  });
  expect([200, 201]).toContain(published.status());
  const revisionHash = (await published.json()).workflow_revision_hash as string;
  const auth = await page.request.post(`${API}/auth-profile-revisions`, {
    data: {
      profile_id: `rail-sticky-${suffix}`,
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post(`${API}/agent-configuration-revisions`, {
    data: {
      model: `rail-sticky-model-${suffix}`,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(page, "e2e-v3", `rail-sticky-model-${suffix}`, agentHash);
  const references = await Promise.all(
    Array.from({ length: COMPLETED_RUN_COUNT }, async (_, index) => {
      const started = await page.request.post(`${API}/runs`, {
        data: {
          workflow_format_version: 3,
          run_id: `rail-sticky/${suffix}/${index}`,
          workflow_revision_hash: revisionHash,
          agent_bindings: [{ role: "builder", agent_configuration_revision_hash: agentHash }],
          orders: []
        }
      });
      expect(started.status()).toBe(201);
      return (await started.json()).public_run_reference as string;
    })
  );
  await expect(async () => {
    const states = await Promise.all(
      references.map(async (reference) => {
        const read = await page.request.get(`${API}/runs/${reference}`);
        return (await read.json()).state as string;
      })
    );
    expect(states.every((state) => state === "COMPLETED")).toBe(true);
  }).toPass({ timeout: 20_000 });
  return workflowName;
}

function settingsEntry(page: Page) {
  return page.getByRole("navigation", { name: "Workshop" }).getByRole("link", {
    name: new RegExp(`${WORKSHOP_DESTINATION.settings.label}[\\s\\S]*${THE_ONE_PROJECT}`)
  });
}

async function overflowingScroller(page: Page): Promise<OverflowScroller> {
  return page.evaluate(() => {
    const stage = document.querySelector(".workshop-stage");
    if (stage !== null && stage.scrollHeight > stage.clientHeight) {
      return {
        kind: "stage" as const,
        scrollHeight: stage.scrollHeight,
        clientHeight: stage.clientHeight
      };
    }
    const scrollingElement = document.scrollingElement ?? document.documentElement;
    return {
      kind: "document" as const,
      scrollHeight: scrollingElement.scrollHeight,
      clientHeight: scrollingElement.clientHeight
    };
  });
}

async function scrollScrollerToEnd(page: Page, kind: OverflowScroller["kind"]): Promise<void> {
  if (kind === "stage") {
    await page.locator(".workshop-stage").evaluate((stage) => {
      stage.scrollTop = stage.scrollHeight - stage.clientHeight;
    });
    return;
  }
  await page.evaluate(() => {
    const scrollingElement = document.scrollingElement ?? document.documentElement;
    scrollingElement.scrollTop = scrollingElement.scrollHeight - scrollingElement.clientHeight;
  });
}

test("Settings stays in view on overflowing History before and after scroll at 1280 and 390", async ({
  page
}, testInfo) => {
  test.setTimeout(120_000);
  const workflowName = await seedOverflowingHistory(page, testInfo.repeatEachIndex);

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await page.goto("/atelier/history");
    await expect(page.getByRole("heading", { name: historyPageCopy.title })).toBeVisible();
    await expect(page.getByRole("link", { name: new RegExp(workflowName) })).toHaveCount(
      COMPLETED_RUN_COUNT
    );

    const settings = settingsEntry(page);
    await expect(settings).toBeVisible();
    const scroller = await overflowingScroller(page);
    expect(
      scroller.scrollHeight,
      `History must overflow at ${viewport.width} (${scroller.kind})`
    ).toBeGreaterThan(scroller.clientHeight);

    await page.screenshot({ path: testInfo.outputPath(`history-${viewport.width}-before.png`) });
    await expect(
      settings,
      `Settings in view at ${viewport.width} before scroll`
    ).toBeInViewport();

    await scrollScrollerToEnd(page, scroller.kind);
    await page.screenshot({ path: testInfo.outputPath(`history-${viewport.width}-after.png`) });
    await expect(
      settings,
      `Settings in view at ${viewport.width} after scroll`
    ).toBeInViewport();
  }
});
