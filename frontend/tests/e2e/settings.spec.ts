import { expect, test, type Page } from "@playwright/test";

import { readStateCopy, retryLabel } from "../../src/lib/readStateCopy";
import { difficultyLabel, settingsPageCopy } from "../../src/lib/settingsPageCopy";

const projectReference = "project1.dGVzdA";
const configurationHash = "a".repeat(64);
const profileHash = "b".repeat(64);
const registryHash = "c".repeat(64);
const defaultsHash = "d".repeat(64);

async function publishCheckedRegistryEntry(
  page: Page,
  providerId: string,
  modelId: string,
  agentConfigurationRevisionHash: string
): Promise<string> {
  const current = await page.request.get(`/atelier/api/v1/model-registries/${providerId}`);
  const currentRegistry = current.status() === 200
    ? await current.json() as {
        revision_number: number;
        model_registry_revision_hash: string;
        entries: Array<{ model_id: string; agent_configuration_revision_hash: string }>;
      }
    : null;
  if (currentRegistry === null) expect(current.status()).toBe(404);
  const existingEntries = (currentRegistry?.entries ?? [])
    .filter((entry) => entry.agent_configuration_revision_hash !== agentConfigurationRevisionHash)
    .map((entry) => ({
      model_id: entry.model_id,
      agent_configuration_revision_hash: entry.agent_configuration_revision_hash
    }));
  const registry = await page.request.put(`/atelier/api/v1/model-registries/${providerId}`, {
    data: {
      revision_number: (currentRegistry?.revision_number ?? 0) + 1,
      entries: [...existingEntries, {
        model_id: modelId,
        agent_configuration_revision_hash: agentConfigurationRevisionHash
      }]
    }
  });
  expect([200, 201]).toContain(registry.status());
  const checked = await page.request.post(`/atelier/api/v1/model-registries/${providerId}/validations`, {
    data: { agent_configuration_revision_hash: agentConfigurationRevisionHash }
  });
  expect([200, 201]).toContain(checked.status());
  const checkedRegistry = await checked.json() as { model_registry_revision_hash: string };
  return checkedRegistry.model_registry_revision_hash;
}

async function publishStartableProvider(
  page: Page,
  providerId: string,
  executorRevision: string,
  stamp: string
): Promise<{
  providerId: string;
  modelId: string;
  agentConfigurationRevisionHash: string;
  modelRegistryRevisionHash: string;
}> {
  const profileId = `settings-${providerId}-${stamp}`;
  const modelId = `settings-${providerId}-model-${stamp}`;
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: profileId,
      revision_number: 1,
      provider_id: providerId,
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: modelId,
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash as string,
      executor_revision: executorRevision,
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);
  const agentConfigurationRevisionHash =
    (await configuration.json()).agent_configuration_revision_hash as string;
  const modelRegistryRevisionHash = await publishCheckedRegistryEntry(
    page,
    providerId,
    modelId,
    agentConfigurationRevisionHash
  );
  return {
    providerId,
    modelId,
    agentConfigurationRevisionHash,
    modelRegistryRevisionHash
  };
}

function delayedReadGate(): { promise: Promise<void>; release: () => void } {
  let release: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  if (release === undefined) throw new Error("delayed settings read has no release");
  return { promise, release };
}

async function routeSettings(
  page: Page,
  providerCheck: "checked" | "unknown-at-provider" = "checked",
  failFirstDefaultsWrite = false
): Promise<{
  registryBodies: string[];
  defaultBodies: string[];
}> {
  const registryBodies: string[] = [];
  const defaultBodies: string[] = [];
  let registryRevision = 1;
  let registryEntries = [{
    model_id: "claude-opus-4-1",
    agent_configuration_revision_hash: configurationHash,
    source: "operator",
    provider_check: providerCheck
  }];

  await page.route("**/atelier/api/v1/projects", (route) => route.fulfill({
    json: { items: [{ public_project_reference: projectReference }] }
  }));
  await page.route("**/atelier/api/v1/projects/*/source-connection", (route) => route.fulfill({
    json: {
      public_project_reference: projectReference,
      revision_number: 2,
      source_kind: "github",
      source_address: "atelier/atelier-2",
      auth_method: "personal-access-token",
      project_source_connection_revision_hash: "e".repeat(64)
    }
  }));
  await page.route("**/atelier/api/v1/agent-configuration-revisions*", (route) => route.fulfill({
    json: {
      items: [{
        model: "claude-opus-4-1",
        auth_profile_revision_hash: profileHash,
        executor_revision: "v1",
        requested_capability: "headless",
        provider_id: "anthropic",
        auth_mode: "subscription",
        agent_configuration_revision_hash: configurationHash,
        startable: providerCheck === "checked",
        not_startable_reason: providerCheck === "checked" ? null : "agent-executor-binding-unavailable"
      }],
      next_after_revision_hash: null
    }
  }));
  await page.route("**/atelier/api/v1/auth-profile-revisions*", (route) => route.fulfill({
    json: {
      items: [{
        profile_id: "Max account",
        revision_number: 1,
        provider_id: "anthropic",
        auth_mode: "subscription",
        auth_profile_revision_hash: profileHash
      }],
      next_after_revision_hash: null
    }
  }));
  await page.route("**/atelier/api/v1/model-registries/anthropic", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: {
        provider_id: "anthropic",
        revision_number: registryRevision,
        model_registry_revision_hash: registryHash,
        entries: registryEntries
      } });
      return;
    }
    const body = route.request().postData() ?? "";
    registryBodies.push(body);
    if (registryBodies.length === 1) {
      await route.fulfill({ status: 503, json: {
        type: "urn:atelier2:problem:v1:temporarily-unavailable",
        title: "Temporarily unavailable",
        status: 503,
        detail: "write outcome uncertain"
      } });
      return;
    }
    const input = JSON.parse(body) as {
      revision_number: number;
      entries: Array<{
        model_id: string;
        agent_configuration_revision_hash: string;
      }>;
    };
    registryRevision = input.revision_number;
    registryEntries = input.entries.map((entry) => ({
      ...entry,
      source: "operator",
      provider_check: "checked" as const
    }));
    await route.fulfill({ json: {
      provider_id: "anthropic",
      revision_number: registryRevision,
      model_registry_revision_hash: "f".repeat(64),
      entries: registryEntries
    } });
  });
  await page.route("**/atelier/api/v1/projects/*/model-defaults", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: {
        project_id: "atelier",
        public_project_reference: projectReference,
        revision_number: 1,
        project_model_defaults_revision_hash: defaultsHash,
        defaults: [{
          difficulty: 3,
          model_registry_revision_hash: registryHash,
          provider_id: "anthropic",
          model_id: "claude-opus-4-1",
          agent_configuration_revision_hash: configurationHash
        }]
      } });
      return;
    }
    const body = route.request().postData() ?? "";
    defaultBodies.push(body);
    if (failFirstDefaultsWrite && defaultBodies.length === 1) {
      await route.fulfill({ status: 503, json: {
        type: "urn:atelier2:problem:v1:temporarily-unavailable",
        title: "Temporarily unavailable",
        status: 503,
        detail: "write outcome uncertain"
      } });
      return;
    }
    const input = JSON.parse(body) as { revision_number: number; defaults: unknown[] };
    await route.fulfill({ json: {
      project_id: "atelier",
      public_project_reference: projectReference,
      revision_number: input.revision_number,
      project_model_defaults_revision_hash: "0".repeat(64),
      defaults: input.defaults
    } });
  });
  return { registryBodies, defaultBodies };
}

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`Settings retains an unavailable saved model until cleared at ${viewport.width}`, async ({ page }) => {
    const writes = await routeSettings(page, "unknown-at-provider");

    await page.setViewportSize(viewport);
    await page.goto("/atelier/settings");
    for (const difficulty of [3, 2, 1]) {
      await expect(page.getByRole("combobox", { name: difficultyLabel(difficulty) })).toBeVisible();
    }
    const selected = page.getByRole("combobox", { name: difficultyLabel(3) });
    const retained = page.getByText(
      "claude-opus-4-1 · Account Max account — Unavailable",
      { exact: true }
    );
    await expect(retained).toBeVisible();
    if (viewport.width === 390) {
      expect(await retained.evaluate((element) => ({
        right: element.getBoundingClientRect().right,
        whiteSpace: getComputedStyle(element).whiteSpace,
        text: element.textContent
      }))).toEqual({
        right: expect.any(Number),
        whiteSpace: "normal",
        text: "claude-opus-4-1 · Account Max account — Unavailable"
      });
      expect(await retained.evaluate((element) => element.getBoundingClientRect().right)).toBeLessThanOrEqual(390);
    }
    await page.screenshot({ path: `test-results/settings-${viewport.width}-unavailable-default.png`, fullPage: true });

    await selected.selectOption("__clear");
    await expect.poll(() => writes.defaultBodies.length).toBe(1);
    expect(JSON.parse(writes.defaultBodies[0] ?? "")).toEqual({ revision_number: 2, defaults: [] });
  });

  test(`Settings retries an exact failed defaults write at ${viewport.width}`, async ({ page }) => {
    const writes = await routeSettings(page, "checked", true);

    await page.setViewportSize(viewport);
    await page.goto("/atelier/settings");
    await page.getByRole("combobox", { name: difficultyLabel(3) }).selectOption("");
    await expect(page.getByText("Change not saved")).toBeVisible();
    await page.getByRole("button", { name: settingsPageCopy.retry }).click();
    await expect.poll(() => writes.defaultBodies.length).toBe(2);
    expect(writes.defaultBodies[1]).toBe(writes.defaultBodies[0]);
  });
}

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`Settings freezes and exactly recovers mutations at ${viewport.width}`, async ({ page }) => {
    const writes = await routeSettings(page);

    await page.setViewportSize(viewport);
    await page.goto("/atelier/settings");
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Model defaults" })).toBeVisible();
    await expect(page.getByText("added by you")).toBeVisible();
    await expect(page.getByText("✓ checked")).toBeVisible();
    await expect(page.getByRole("combobox", { name: difficultyLabel(3) }).locator("option:checked"))
      .toHaveText("claude-opus-4-1 · Account Max account");
    await expect(page.getByText(/Saving|Saved/)).toHaveCount(0);
    await expect(page.getByText("Work in this project")).toHaveCount(0);
    if (viewport.width === 1280) {
      await expect(page.getByRole("cell", { name: "Max account", exact: true })).toBeVisible();
    }
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-mutation-ready.png`,
      fullPage: true
    });

    await page.getByRole("combobox", { name: difficultyLabel(3) }).selectOption("");
    await expect.poll(() => writes.defaultBodies.length).toBe(1);
    expect(JSON.parse(writes.defaultBodies[0] ?? "")).toEqual({
      revision_number: 2,
      defaults: []
    });

    await page.getByRole("button", { name: settingsPageCopy.remove }).click();
    await expect(page.getByText("Change not saved")).toBeVisible();
    await expect(page.getByRole("combobox", { name: difficultyLabel(3) })).toBeDisabled();
    await page.getByRole("combobox", { name: difficultyLabel(3) }).evaluate((element) => {
      const select = element as HTMLSelectElement;
      select.value = "";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await expect.poll(() => writes.defaultBodies.length).toBe(1);
    await page.getByRole("button", { name: settingsPageCopy.retry }).click();
    await expect.poll(() => writes.registryBodies.length).toBe(2);
    expect(writes.registryBodies[1]).toBe(writes.registryBodies[0]);
    await expect(page.getByRole("combobox", { name: settingsPageCopy.addModel })).toBeVisible();
  });
}

test("Settings tells the truth while reads load, fail, and recover at desktop and mobile", async ({ page }) => {
  await routeSettings(page);
  let reply: "delayed" | "unavailable" | "available" = "delayed";
  let delayedRead = delayedReadGate();
  await page.route("**/atelier/api/v1/projects", async (route) => {
    if (reply === "delayed") {
      await delayedRead.promise;
    }
    if (reply === "unavailable") {
      await route.fulfill({ status: 503, json: {
        type: "urn:atelier2:problem:v1:temporarily-unavailable",
        title: "Temporarily unavailable",
        status: 503,
        detail: "settings read unavailable"
      } });
      return;
    }
    await route.fulfill({ json: { items: [{ public_project_reference: projectReference }] } });
  });

  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    reply = "delayed";
    delayedRead = delayedReadGate();
    const navigation = page.goto(`/atelier/settings?state=loading-${viewport.width}`);
    await expect(page.getByText(readStateCopy.looking)).toBeVisible();
    delayedRead.release();
    await navigation;
    await expect(page.getByRole("heading", { name: "Model defaults" })).toBeVisible();
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-loaded.png`,
      fullPage: true
    });

    reply = "unavailable";
    await page.reload();
    await expect(page.getByRole("alert")).toContainText("Settings unavailable");
    await expect(page.getByText("settings read unavailable")).toHaveCount(0);
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-unavailable.png`,
      fullPage: true
    });

    reply = "available";
    await page.getByRole("button", { name: retryLabel(settingsPageCopy.label) }).click();
    await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
    await expect(page.getByRole("combobox", { name: difficultyLabel(3) }).locator("option:checked"))
      .toHaveText("claude-opus-4-1 · Account Max account");
  }
});

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`Settings lists every startable model and saves a default at ${viewport.width}`, async ({ page }) => {
  const stamp = `${Date.now()}-${test.info().repeatEachIndex}-${viewport.width}`;
  const first = await publishStartableProvider(page, "e2e-v3", "immediate/v1", stamp);
  const second = await publishStartableProvider(page, "e2e-v3-slow", "delayed/v1", stamp);
  const projects = await page.request.get("/atelier/api/v1/projects");
  expect(projects.status()).toBe(200);
  const servedProjectReference = (await projects.json() as {
    items: Array<{ public_project_reference: string }>;
  }).items[0]?.public_project_reference;
  expect(servedProjectReference).toEqual(expect.any(String));

  const putBodies: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && request.url().includes("/model-defaults")) {
      putBodies.push(request.postData() ?? "");
    }
  });

  await page.setViewportSize(viewport);
  await page.goto("/atelier/settings");
  await expect(page.getByText(first.providerId, { exact: true })).toBeVisible();
  await expect(page.getByText(second.providerId, { exact: true })).toBeVisible();
  await expect(page.getByText(first.modelId, { exact: true })).toBeVisible();
  await expect(page.getByText(second.modelId, { exact: true })).toBeVisible();

  for (const difficulty of [3, 2, 1]) {
    const select = page.getByRole("combobox", { name: difficultyLabel(difficulty) });
    await expect(select.locator(`option[value="${first.agentConfigurationRevisionHash}"]`)).toHaveCount(1);
    await expect(select.locator(`option[value="${second.agentConfigurationRevisionHash}"]`)).toHaveCount(1);
  }

  await page.getByRole("combobox", { name: difficultyLabel(3) }).selectOption(
    first.agentConfigurationRevisionHash
  );
  await expect.poll(() => putBodies.length).toBe(1);
  await expect(page.getByRole("combobox", { name: difficultyLabel(3) })).toBeEnabled();
  await expect(page.getByText("Change not saved")).toHaveCount(0);
  const body = JSON.parse(putBodies[0] ?? "") as {
    revision_number: number;
    defaults: Array<{
      difficulty: number;
      model_registry_revision_hash: string;
      provider_id: string;
      model_id: string;
      agent_configuration_revision_hash: string;
    }>;
  };
  expect(Object.keys(body).sort()).toEqual(["defaults", "revision_number"]);
  expect(body.revision_number).toBeGreaterThanOrEqual(1);
  expect(body.defaults.length).toBeGreaterThanOrEqual(1);
  expect(body.defaults.length).toBeLessThanOrEqual(3);
  for (const item of body.defaults) {
    expect(Object.keys(item).sort()).toEqual([
      "agent_configuration_revision_hash",
      "difficulty",
      "model_id",
      "model_registry_revision_hash",
      "provider_id"
    ]);
    expect([1, 2, 3]).toContain(item.difficulty);
    expect(item.model_registry_revision_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(item.agent_configuration_revision_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(item.provider_id).toMatch(/^[a-z][a-z0-9._-]{0,63}$/);
    expect(item.model_id).toMatch(/^\S+$/);
  }
  expect(body.defaults).toEqual(expect.arrayContaining([{
    difficulty: 3,
    model_registry_revision_hash: first.modelRegistryRevisionHash,
    provider_id: first.providerId,
    model_id: first.modelId,
    agent_configuration_revision_hash: first.agentConfigurationRevisionHash
  }]));
  await expect(page.getByRole("combobox", { name: difficultyLabel(3) }).locator("option:checked"))
    .toHaveText(new RegExp(first.modelId));

  const saved = await page.request.get(
    `/atelier/api/v1/projects/${servedProjectReference}/model-defaults`
  );
  expect(saved.status()).toBe(200);
  const savedBody = await saved.json() as {
    defaults: Array<{
      difficulty: number;
      provider_id: string;
      model_id: string;
      agent_configuration_revision_hash: string;
    }>;
  };
  expect(savedBody.defaults).toEqual(expect.arrayContaining([{
    difficulty: 3,
    model_registry_revision_hash: first.modelRegistryRevisionHash,
    provider_id: first.providerId,
    model_id: first.modelId,
    agent_configuration_revision_hash: first.agentConfigurationRevisionHash
  }]));
});
}
