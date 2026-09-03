import { expect, test, type Page } from "@playwright/test";

import { readStateCopy, retryLabel } from "../../src/lib/readStateCopy";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import {
  accountChoice,
  difficultyLabel,
  disconnectTitle,
  noSuchModel,
  providerAccount,
  retainedAccountChoice,
  settingsPageCopy
} from "../../src/lib/settingsPageCopy";

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
  profileId: string;
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
    profileId,
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
  await page.route("**/atelier/api/v1/projects/*/sources", (route) => {
    if (route.request().method() !== "GET") {
      return route.fallback();
    }
    return route.fulfill({
      json: {
        items: [{
          public_source_reference: "source1.MzgwZjI3YTEtNmRlMC01NjNkLTQwYWItYzg1MzBmOWMyNWNj",
          kind: "github",
          address: "FlexOr2/atelier-2",
          scope: "issues",
          connected_at: null,
          revision: 2,
          auth_method: "personal-access-token"
        }]
      }
    });
  });
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
        structurally_startable: providerCheck === "checked",
        not_startable_reason: providerCheck === "checked" ? null : "agent-executor-binding-unavailable",
        provider_probe_problem_code: null,
        provider_probe_observed_at: null
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
      retainedAccountChoice("claude-opus-4-1", "Max account"),
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
        text: retainedAccountChoice("claude-opus-4-1", "Max account")
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
    await expect(page.getByText(settingsPageCopy.addedByYouChecked)).toBeVisible();
    await expect(page.getByText("✓ checked")).toBeVisible();
    await expect(page.getByRole("combobox", { name: difficultyLabel(3) }).locator("option:checked"))
      .toHaveText(accountChoice("claude-opus-4-1", "Max account"));
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
    await expect(page.getByRole("button", { name: settingsPageCopy.addModel })).toBeVisible();
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
      .toHaveText(accountChoice("claude-opus-4-1", "Max account"));
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
  await expect(
    page.locator(".provider-account-row").getByText(
      providerAccount(first.providerId, first.profileId),
      { exact: true }
    )
  ).toBeVisible();
  await expect(
    page.locator(".provider-account-row").getByText(
      providerAccount(second.providerId, second.profileId),
      { exact: true }
    )
  ).toBeVisible();
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

function modelsRow(page: Page, modelId: string) {
  const models = page.locator(".settings-block").filter({
    has: page.getByRole("heading", { name: settingsPageCopy.modelsTitle })
  });
  return models.getByRole("row").filter({ hasText: modelId }).or(
    models.locator("tr").filter({ hasText: modelId })
  );
}

test("proves(settings-adds-a-model-by-id-and-shows-its-check-state): Settings adds a model by id and shows its check state at 1280 and 390", async ({ page }) => {
  test.setTimeout(180_000);
  const stamp = Date.now();
  const fixture = await publishStartableProvider(page, "e2e-v3", "immediate/v1", `add-${stamp}`);

  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    const newId = `added-model-${stamp}-${viewport.width}`;
    await page.setViewportSize(viewport);
    await page.goto("/atelier/settings");
    await expect(page.getByRole("heading", { name: settingsPageCopy.modelsTitle })).toBeVisible();
    await expect(page.getByText(fixture.modelId, { exact: true })).toBeVisible();

    const addDoor = page.getByRole("button", { name: settingsPageCopy.addModel, exact: true });
    await addDoor.click();
    const dialog = page.getByRole("dialog", { name: settingsPageCopy.addModel });
    await expect(dialog).toBeVisible();

    const box = await dialog.boundingBox();
    expect(box).not.toBeNull();
    if (box === null) throw new Error("add-model sheet has no box");
    if (viewport.width === 1280) {
      expect(Math.abs(viewport.width - (box.x + box.width))).toBeLessThanOrEqual(24);
    } else {
      expect(Math.abs(viewport.height - (box.y + box.height))).toBeLessThanOrEqual(24);
    }

    await expect(dialog.getByRole("combobox", { name: settingsPageCopy.provider })).toContainText(
      fixture.providerId
    );
    await dialog.getByRole("textbox", { name: settingsPageCopy.model }).fill(newId);
    const add = dialog.getByRole("button", { name: settingsPageCopy.add, exact: true });
    await expect(add).toBeEnabled();
    await add.click();
    await expect(dialog).toHaveCount(0);

    await expect(page.getByText(newId, { exact: true })).toHaveCount(1);
    const row = modelsRow(page, newId);
    const checkState = row.getByText(settingsPageCopy.checking, { exact: true })
      .or(row.getByText(settingsPageCopy.addedByYouChecked, { exact: true }))
      .or(row.getByText(noSuchModel(fixture.providerId), { exact: true }));
    await expect(checkState).toBeVisible();
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-add-model.png`,
      fullPage: true
    });

    await expect(row.getByText(settingsPageCopy.checking, { exact: true })).toHaveCount(0, {
      timeout: 30_000
    });
    await expect(
      row.getByText(settingsPageCopy.addedByYouChecked, { exact: true })
        .or(row.getByText(noSuchModel(fixture.providerId), { exact: true }))
    ).toBeVisible();

    await addDoor.click();
    await expect(dialog).toBeVisible();
    await dialog.getByRole("textbox", { name: settingsPageCopy.model }).fill(newId);
    await expect(add).toBeEnabled();
    await add.click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByText(newId, { exact: true })).toHaveCount(1);
    await expect(page.getByText(settingsPageCopy.alreadyPresent)).toBeVisible();

    await row.getByRole("button", { name: settingsPageCopy.remove }).click();
    await expect(page.getByText(newId, { exact: true })).toHaveCount(0);
    await expect(page.getByText(fixture.modelId, { exact: true })).toBeVisible();
  }
});


const picturedAddress = "FlexOr2/atelier-2";
const picturedSource = {
  public_source_reference: "source1.MzgwZjI3YTEtNmRlMC01NjNkLTQwYWItYzg1MzBmOWMyNWNj",
  kind: "github",
  address: picturedAddress,
  scope: "issues",
  connected_at: null,
  revision: 2,
  auth_method: "personal-access-token"
};

async function routePicturedSourceDoors(
  page: Page,
  successfulWrite?: { promise: Promise<void>; release: () => void }
): Promise<void> {
  await routeSettings(page);
  let items: Array<typeof picturedSource> = [];
  await page.route("**/atelier/api/v1/projects/*/sources", async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      await route.fulfill({ json: { items } });
      return;
    }
    if (method === "POST") {
      const body = JSON.parse(route.request().postData() ?? "") as {
        address?: string;
        token?: string;
        kind?: string;
      };
      expect(Object.keys(body).sort()).toEqual(["address", "token"]);
      expect(body.kind).toBeUndefined();
      expect(typeof body.token).toBe("string");
      expect(body.token?.length).toBeGreaterThan(0);
      if (body.token === "refused-token") {
        await route.fulfill({
          status: 422,
          contentType: "application/problem+json",
          json: {
            type: "urn:atelier2:problem:v1:project-source-token-refused",
            title: "Project source token refused",
            status: 422,
            detail: "The provider refused this token."
          }
        });
        return;
      }
      if (successfulWrite !== undefined) {
        await successfulWrite.promise;
      }
      items = [{ ...picturedSource, address: body.address ?? picturedAddress, revision: 1 }];
      await route.fulfill({ status: 201, json: items[0] });
      return;
    }
    await route.fallback();
  });
  await page.route("**/atelier/api/v1/projects/*/sources/*/token", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.fallback();
      return;
    }
    const body = JSON.parse(route.request().postData() ?? "") as { token?: string };
    expect(Object.keys(body)).toEqual(["token"]);
    expect(typeof body.token).toBe("string");
    if (body.token === "refused-token") {
      await route.fulfill({
        status: 422,
        contentType: "application/problem+json",
        json: {
          type: "urn:atelier2:problem:v1:project-source-token-refused",
          title: "Project source token refused",
          status: 422,
          detail: "The provider refused this token."
        }
      });
      return;
    }
    const current = items[0] ?? picturedSource;
    await route.fulfill({ json: current });
  });
  await page.route("**/atelier/api/v1/projects/*/sources/*", async (route) => {
    if (route.request().method() !== "DELETE") {
      await route.fallback();
      return;
    }
    items = [];
    await route.fulfill({ status: 204, body: "" });
  });
}

test("proves(settings-source-doors-follow-the-blessed-picture): Settings connects, shows one source row, disconnects after naming what stays, renews the token, and shows a refused-token error", async ({ page }) => {
  test.setTimeout(120_000);

  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    const successfulWrite = delayedReadGate();
    await routePicturedSourceDoors(page, successfulWrite);
    await page.setViewportSize(viewport);
    await page.goto("/atelier/settings");
    await expect(page.getByRole("heading", { name: settingsPageCopy.sourcesTitle })).toBeVisible();
    await expect(page.getByRole("button", { name: settingsPageCopy.connectASource })).toBeVisible();
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-source-doors-empty.png`,
      fullPage: true
    });

    await page.getByRole("button", { name: settingsPageCopy.connectASource }).click();
    const connectDialog = page.getByRole("dialog", { name: settingsPageCopy.connectASource });
    await expect(connectDialog).toBeVisible();
    const box = await connectDialog.boundingBox();
    expect(box).not.toBeNull();
    if (box === null) throw new Error("connect-source sheet has no box");
    if (viewport.width === 1280) {
      expect(Math.abs(viewport.width - (box.x + box.width))).toBeLessThanOrEqual(24);
    } else {
      expect(Math.abs(viewport.height - (box.y + box.height))).toBeLessThanOrEqual(24);
    }
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-source-doors-connect.png`,
      fullPage: true
    });

    await connectDialog.getByRole("textbox", { name: settingsPageCopy.where }).fill(picturedAddress);
    await connectDialog.getByLabel(settingsPageCopy.token).fill("refused-token");
    await connectDialog.getByRole("button", { name: settingsPageCopy.connect, exact: true }).click();
    await expect(connectDialog.getByText(settingsPageCopy.tokenRefused)).toBeVisible();
    await expect(connectDialog.getByRole("button", { name: settingsPageCopy.renewToken })).toBeVisible();
    await expect(connectDialog.getByLabel(settingsPageCopy.token)).toHaveAttribute("type", "password");
    await expect(connectDialog).not.toContainText("refused-token");

    await connectDialog.getByLabel(settingsPageCopy.token).fill("good-token");
    const connectWrite = connectDialog.getByRole("button", { name: settingsPageCopy.connect, exact: true }).click();
    try {
      await expect(connectDialog.getByText(settingsPageCopy.running)).toBeVisible();
    } finally {
      successfulWrite.release();
    }
    await connectWrite;
    await expect(connectDialog).toHaveCount(0);

    const row = page.locator(".source-row").filter({ hasText: picturedAddress });
    await expect(row).toBeVisible();
    await expect(row.getByText(`${settingsPageCopy.github} · ${picturedAddress}`)).toBeVisible();
    await expect(row).toContainText(settingsPageCopy.issues);
    await expect(row).toContainText(settingsPageCopy.connectionTimeNotRecorded);
    await expect(row).not.toContainText("personal-access-token");
    await expect(row).not.toContainText("@");
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-source-doors-row.png`,
      fullPage: true
    });

    await row.getByRole("button", { name: settingsPageCopy.renewToken }).click();
    const renewDialog = page.getByRole("dialog", { name: settingsPageCopy.renewToken });
    await expect(renewDialog).toBeVisible();
    await renewDialog.getByLabel(settingsPageCopy.token).fill("next-token");
    await renewDialog.getByRole("button", { name: settingsPageCopy.renewToken, exact: true }).click();
    await expect(renewDialog).toHaveCount(0);
    await expect(row.getByText(`${settingsPageCopy.github} · ${picturedAddress}`)).toBeVisible();
    await expect(row).toContainText(settingsPageCopy.connectionTimeNotRecorded);

    await row.getByRole("button", { name: settingsPageCopy.renewToken }).click();
    await expect(renewDialog).toBeVisible();
    await renewDialog.getByLabel(settingsPageCopy.token).fill("refused-token");
    await renewDialog.getByRole("button", { name: settingsPageCopy.renewToken, exact: true }).click();
    await expect(renewDialog.getByText(settingsPageCopy.tokenRefused)).toBeVisible();
    await expect(renewDialog.getByRole("button", { name: settingsPageCopy.renewToken }).nth(1)).toBeVisible();
    await expect(renewDialog.getByLabel(settingsPageCopy.token)).toHaveAttribute("type", "password");
    await renewDialog.getByRole("button", { name: settingsPageCopy.cancel }).click();
    await expect(renewDialog).toHaveCount(0);

    await row.getByRole("button", { name: settingsPageCopy.disconnect }).click();
    const disconnectDialog = page.getByRole("dialog", { name: disconnectTitle(picturedAddress) });
    await expect(disconnectDialog).toBeVisible();
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-source-doors-disconnect.png`,
      fullPage: true
    });
    await expect(disconnectDialog).toContainText(settingsPageCopy.thisConnection);
    await expect(disconnectDialog).toContainText(THE_ONE_PROJECT);
    await expect(disconnectDialog).toContainText(settingsPageCopy.theModels);
    await disconnectDialog.getByRole("button", { name: settingsPageCopy.disconnect }).click();
    await expect(disconnectDialog).toHaveCount(0);
    await expect(page.locator(".source-row")).toHaveCount(0);
    const sourcePath = `/atelier/api/v1/projects/${projectReference}/sources/${picturedSource.public_source_reference}`;
    const repeatedDelete = await page.evaluate(async (path) => {
      const response = await fetch(path, { method: "DELETE" });
      return response.status;
    }, sourcePath);
    expect(repeatedDelete).toBe(204);
    await page.reload();
    await expect(page.getByRole("heading", { name: settingsPageCopy.modelsTitle })).toBeVisible();
    await expect(page.getByRole("button", { name: settingsPageCopy.connectASource })).toBeVisible();
    await expect(page.locator(".source-row")).toHaveCount(0);
  }
});

async function routeEmptyProviderAccounts(page: Page): Promise<void> {
  await routeSettings(page);
  await page.route("**/atelier/api/v1/auth-profile-revisions*", (route) => route.fulfill({
    json: { items: [], next_after_revision_hash: null }
  }));
  await page.route("**/atelier/api/v1/agent-configuration-revisions*", (route) => route.fulfill({
    json: { items: [], next_after_revision_hash: null }
  }));
  await page.route("**/atelier/api/v1/projects/*/sources", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({ json: { items: [] } });
  });
  await page.route("**/atelier/api/v1/model-registries/anthropic", (route) => route.fulfill({
    status: 404,
    contentType: "application/problem+json",
    json: {
      type: "urn:atelier2:problem:v1:model-registry-missing",
      title: "Model registry missing",
      status: 404,
      detail: "missing"
    }
  }));
  await page.route("**/atelier/api/v1/projects/*/model-defaults", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({
      status: 404,
      contentType: "application/problem+json",
      json: {
        type: "urn:atelier2:problem:v1:project-model-defaults-missing",
        title: "Project model defaults not found",
        status: 404,
        detail: "missing"
      }
    });
  });
}

function checkedOperatorRegistry() {
  return {
    provider_id: "anthropic",
    revision_number: 2,
    model_registry_revision_hash: "f".repeat(64),
    entries: [{
      model_id: "claude-opus-4-1",
      agent_configuration_revision_hash: configurationHash,
      source: "operator" as const,
      provider_check: "checked" as const
    }]
  };
}

test("proves(settings-shows-the-provider-account-as-its-own-thing): Settings shows the provider account as its own thing at 1280 and 390", async ({ page }) => {
  test.setTimeout(180_000);

  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);

    await routeEmptyProviderAccounts(page);
    await page.goto("/atelier/settings");
    const sources = page.getByRole("heading", { name: settingsPageCopy.sourcesTitle });
    const accounts = page.getByRole("heading", { name: settingsPageCopy.account, exact: true });
    const models = page.getByRole("heading", { name: settingsPageCopy.modelsTitle });
    await expect(sources).toBeVisible();
    await expect(accounts).toBeVisible();
    await expect(models).toBeVisible();
    const sourceBox = await sources.boundingBox();
    const accountBox = await accounts.boundingBox();
    const modelBox = await models.boundingBox();
    expect(sourceBox).not.toBeNull();
    expect(accountBox).not.toBeNull();
    expect(modelBox).not.toBeNull();
    if (sourceBox === null || accountBox === null || modelBox === null) {
      throw new Error("settings headings have no box");
    }
    expect(sourceBox.y).toBeLessThan(accountBox.y);
    expect(accountBox.y).toBeLessThan(modelBox.y);
    await expect(page.locator(".provider-account-row")).toHaveCount(0);
    await expect(page.getByRole("button", { name: settingsPageCopy.addModel, exact: true })).toBeVisible();
    for (const difficulty of [3, 2, 1]) {
      await expect(page.getByRole("combobox", { name: difficultyLabel(difficulty) })).toBeVisible();
    }
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-provider-accounts-empty.png`,
      fullPage: true
    });

    await routeSettings(page);
    const validationsHold = delayedReadGate();
    let refuseValidation = false;
    await page.route("**/atelier/api/v1/model-registries/**/validations", async (route) => {
      if (route.request().method() !== "POST") {
        await route.fallback();
        return;
      }
      if (refuseValidation) {
        await route.fulfill({
          status: 503,
          contentType: "application/problem+json",
          json: {
            type: "urn:atelier2:problem:v1:temporarily-unavailable",
            title: "Temporarily unavailable",
            status: 503,
            detail: "validation outcome uncertain"
          }
        });
        return;
      }
      await validationsHold.promise;
      await route.fulfill({ status: 201, json: checkedOperatorRegistry() });
    });
    await page.goto("/atelier/settings");
    const accountRow = page.locator(".provider-account-row");
    await expect(accountRow).toHaveCount(1);
    await expect(accountRow.getByText(providerAccount("anthropic", "Max account"), { exact: true })).toBeVisible();
    await expect(accountRow.getByText(settingsPageCopy.neverShownAgain, { exact: true })).toBeVisible();
    await expect(page.locator(".provider-account-rows input[type=password]")).toHaveCount(0);
    await expect(page.getByText("secret-token-value")).toHaveCount(0);
    await page.screenshot({
      path: `test-results/settings-${viewport.width}-provider-accounts.png`,
      fullPage: true
    });

    const modelRow = modelsRow(page, "claude-opus-4-1");
    const check = modelRow.getByRole("button", { name: settingsPageCopy.check });
    await expect(check).toBeVisible();
    const checkClick = check.click();
    await expect(modelRow.getByText(settingsPageCopy.checking, { exact: true })).toBeVisible();
    validationsHold.release();
    await checkClick;
    await expect(modelRow.getByText(settingsPageCopy.checking, { exact: true })).toHaveCount(0);
    await expect(modelRow.getByText(settingsPageCopy.addedByYouChecked, { exact: true })).toBeVisible();

    await page.getByRole("button", { name: settingsPageCopy.addModel, exact: true }).click();
    const dialog = page.getByRole("dialog", { name: settingsPageCopy.addModel });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("textbox", { name: settingsPageCopy.model }).fill("claude-opus-4-1");
    await dialog.getByRole("button", { name: settingsPageCopy.add, exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByText("claude-opus-4-1", { exact: true })).toHaveCount(1);
    await expect(modelRow.getByText(settingsPageCopy.alreadyPresent)).toBeVisible();

    refuseValidation = true;
    await check.click();
    const failure = modelRow.getByRole("alert");
    await expect(failure).toContainText(settingsPageCopy.writeFailed);
    await expect(page.getByRole("button", { name: settingsPageCopy.retry })).toHaveCount(1);
  }
});

