import { expect, test, type Page } from "@playwright/test";

const projectReference = "project1.dGVzdA";
const configurationHash = "a".repeat(64);
const profileHash = "b".repeat(64);
const registryHash = "c".repeat(64);
const defaultsHash = "d".repeat(64);

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
        startable: false,
        not_startable_reason: "agent-executor-binding-unavailable"
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
    await page.goto("/atelier/project");
    for (const difficulty of [3, 2, 1]) {
      await expect(page.getByRole("combobox", { name: `Difficulty ${difficulty}` })).toBeVisible();
    }
    const selected = page.getByRole("combobox", { name: "Difficulty 3" });
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
    await page.goto("/atelier/project");
    await page.getByRole("combobox", { name: "Difficulty 3" }).selectOption("");
    await expect(page.getByText("Change not saved")).toBeVisible();
    await page.getByRole("button", { name: "Retry" }).click();
    await expect.poll(() => writes.defaultBodies.length).toBe(2);
    expect(writes.defaultBodies[1]).toBe(writes.defaultBodies[0]);
  });
}

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`Settings freezes and exactly recovers mutations at ${viewport.width}`, async ({ page }) => {
    const writes = await routeSettings(page);

    await page.setViewportSize(viewport);
    await page.goto("/atelier/project");
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Model defaults" })).toBeVisible();
    await expect(page.getByText("added by you")).toBeVisible();
    await expect(page.getByText("✓ checked")).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Difficulty 3" }).locator("option:checked"))
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

    await page.getByRole("combobox", { name: "Difficulty 3" }).selectOption("");
    await expect.poll(() => writes.defaultBodies.length).toBe(1);
    expect(JSON.parse(writes.defaultBodies[0] ?? "")).toEqual({
      revision_number: 2,
      defaults: []
    });

    await page.getByRole("button", { name: "Remove" }).click();
    await expect(page.getByText("Change not saved")).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Difficulty 3" })).toBeDisabled();
    await page.getByRole("combobox", { name: "Difficulty 3" }).evaluate((element) => {
      const select = element as HTMLSelectElement;
      select.value = "";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await expect.poll(() => writes.defaultBodies.length).toBe(1);
    await page.getByRole("button", { name: "Retry" }).click();
    await expect.poll(() => writes.registryBodies.length).toBe(2);
    expect(writes.registryBodies[1]).toBe(writes.registryBodies[0]);
    await expect(page.getByRole("combobox", { name: "Add a model" })).toBeVisible();
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
    const navigation = page.goto(`/atelier/project?state=loading-${viewport.width}`);
    await expect(page.getByText("Looking…")).toBeVisible();
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
    await page.getByRole("button", { name: "Retry settings" }).click();
    await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Difficulty 3" }).locator("option:checked"))
      .toHaveText("claude-opus-4-1 · Account Max account");
  }
});
