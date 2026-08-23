import { expect, test, type Page } from "@playwright/test";

/**
 * The mockup-comparison screenshots of every surface, at both widths.
 *
 * Not a gate: an evidence run the operator asked for (REQ-UIQ-09's ritual).
 * It is skipped unless ATELIER2_SHOT_DIR names where the images go.
 */
const shotDir = process.env.ATELIER2_SHOT_DIR ?? "";

test.skip(shotDir === "", "no shot directory named");

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;

async function shoot(page: Page, name: string): Promise<void> {
  for (const viewport of widths) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.waitForTimeout(250);
    await page.screenshot({ path: `${shotDir}/${name}-${viewport.name}.png`, fullPage: true });
  }
}

async function anyJsonSchema(page: Page): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"$schema":"https://json-schema.org/draft/2020-12/schema"}'
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

async function immediateAgent(page: Page): Promise<string> {
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: {
      profile_id: "shots",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "shot-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  return (await configuration.json()).agent_configuration_revision_hash as string;
}

test("captures every surface at both widths", async ({ page }) => {
  const schemaHash = await anyJsonSchema(page);
  const agentHash = await immediateAgent(page);

  const iterate = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: iterate-code",
      "description: build → review → fix, until green",
      "nodes:",
      "  - id: build",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Build the candidate.",
      `    outputs: [{name: draft, schema: {ref: any, revision: ${schemaHash}}}]`,
      "  - id: review",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Review the candidate.",
      "    depends_on: [build]",
      `    outputs: [{name: verdict, schema: {ref: any, revision: ${schemaHash}}}]`,
      "  - id: gate",
      "    type: wait",
      "    prompt: The review is green. Merge this, or name the blocking defect.",
      "    depends_on: [review]",
      `    outputs: [{name: decision, schema: {ref: any, revision: ${schemaHash}}}]`,
      ""
    ].join("\n")
  });
  expect(iterate.status()).toBe(201);
  const iterateHash = (await iterate.json()).workflow_revision_hash as string;
  expect(
    (
      await page.request.post("/atelier/api/v1/workflow-lineages", {
        data: {
          workflow_revision_hash: iterateHash,
          actor: "shots",
          activated_at: "2026-08-23T00:00:00Z"
        }
      })
    ).status()
  ).toBe(201);

  const started = await page.request.post("/atelier/api/v1/runs", {
    data: {
      workflow_format_version: 3,
      run_id: "demo/waiting-gate",
      workflow_revision_hash: iterateHash,
      agent_bindings: [{ role: "builder", agent_configuration_revision_hash: agentHash }],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe("WAITING_INPUT");
  }).toPass({ timeout: 20_000 });

  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: "Chat" })).toBeVisible();
  await shoot(page, "chat-empty");
  await page.getByLabel("Message").fill("Finish the preview door and fix the wait bug, in parallel.");
  await page.getByRole("button", { name: "Send" }).click();
  await shoot(page, "chat-said");

  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
  await shoot(page, "board");

  await page.goto("/atelier/workflows");
  await expect(page.getByRole("button", { name: "iterate-code" })).toBeVisible();
  await shoot(page, "workflows");

  await page.goto("/atelier/workflows/iterate-code");
  await expect(page.getByRole("heading", { level: 1, name: "iterate-code" })).toBeVisible();
  await shoot(page, "workflow-detail");

  await page.goto("/atelier/new");
  await expect(page.getByRole("heading", { name: "Choose a workflow" })).toBeVisible();
  await shoot(page, "new-run");

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByLabel("Where this run stands")).toContainText("Waiting for you");
  await shoot(page, "run-waiting");

  await page.getByRole("button", { name: /build/ }).click();
  await expect(page.getByRole("tablist")).toBeVisible();
  await shoot(page, "run-node-tabs");
  await page.getByRole("tab", { name: "Evidence" }).click();
  await shoot(page, "run-node-evidence");

  await page.goto(`/atelier/runs/${reference}`);
  await page.getByLabel("Your answer").fill("merge it");
  await page.getByRole("button", { name: "Answer" }).click();
  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 20_000 });
  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByLabel("Where this run stands")).toContainText("Done");
  await shoot(page, "run-answered");

  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
  await shoot(page, "board-populated");

  await page.goto("/atelier/history");
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
  await shoot(page, "history");

  await page.goto("/atelier/project");
  await expect(page.getByRole("heading", { name: "atelier-2" })).toBeVisible();
  await shoot(page, "project");
});
