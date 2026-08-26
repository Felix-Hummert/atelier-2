import { expect, test, type Page } from "@playwright/test";

import { THE_ONE_PROJECT } from "../../src/lib/project";

/**
 * The mockup-comparison screenshots of every surface, at both widths and in
 * both themes.
 *
 * Not a gate: an evidence run the operator asked for (REQ-UIQ-09's ritual).
 * It is skipped unless ATELIER2_SHOT_DIR names where the images go.
 */
const shotDir = process.env.ATELIER2_SHOT_DIR ?? "";

test.skip(shotDir === "", "no shot directory named");

// Not a gate but a sitting: two themes, two widths, every surface, and two
// runs staged live. It is allowed to take as long as that honestly takes.
test.setTimeout(300_000);

const widths = [
  { name: "1280", width: 1280, height: 900 },
  { name: "390", width: 390, height: 844 }
] as const;

/** Light and dark are skinned with one care, so both are photographed. */
const themes = ["light", "dark"] as const;

async function shoot(page: Page, name: string): Promise<void> {
  for (const theme of themes) {
    await page.emulateMedia({ colorScheme: theme });
    for (const viewport of widths) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.waitForTimeout(250);
      await page.screenshot({
        path: `${shotDir}/${theme}/${name}-${viewport.name}.png`,
        fullPage: true
      });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
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

/**
 * A run that fails, and one that is still working, so the evidence series can
 * show the two states a calm Board is judged on: brick that is unmistakably
 * not clay, and the blue that means something is actually running.
 */
async function agentOf(
  page: Page,
  profileId: string,
  providerId: string,
  executorRevision: string
): Promise<string> {
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: { profile_id: profileId, revision_number: 1, provider_id: providerId, auth_mode: "subscription" }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "shot-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: executorRevision,
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  return (await configuration.json()).agent_configuration_revision_hash as string;
}

async function chainOf(page: Page, name: string, schemaHash: string, nodeIds: readonly string[]): Promise<string> {
  const lines = ["format_version: 3", `name: ${name}`, "nodes:"];
  nodeIds.forEach((nodeId, index) => {
    lines.push(
      `  - id: ${nodeId}`,
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      `    instruction: Do the ${nodeId} step.`,
      ...(index === 0 ? [] : [`    depends_on: [${nodeIds[index - 1]}]`]),
      `    outputs: [{name: ${nodeId}_result, schema: {ref: any, revision: ${schemaHash}}}]`
    );
  });
  const published = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: `${lines.join("\n")}\n`
  });
  expect(published.status()).toBe(201);
  return (await published.json()).workflow_revision_hash as string;
}

async function startRun(page: Page, runId: string, revisionHash: string, agentHash: string): Promise<string> {
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
  return (await started.json()).public_run_reference as string;
}

async function runReaches(page: Page, reference: string, state: string): Promise<void> {
  await expect(async () => {
    const read = await page.request.get(`/atelier/api/v1/runs/${reference}`);
    expect((await read.json()).state).toBe(state);
  }).toPass({ timeout: 30_000 });
}

test("captures every surface at both widths", async ({ page }) => {
  const schemaHash = await anyJsonSchema(page);
  const agentHash = await immediateAgent(page);

  // The Workbench before anything needs a person: the pinned region greets
  // rather than apologises, and the composer is already within reach (#580).
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await expect(page.getByText("Nothing needs you right now.")).toBeVisible();
  await shoot(page, "workbench-empty");

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
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  // The waiting run staged above is pinned here: the decision that needs a
  // person, held in the "Needs you" region so it never scrolls away (#580).
  await expect(page.getByRole("heading", { name: "The review is green. Merge this, or name the blocking defect." })).toBeVisible();
  await shoot(page, "workbench-needs-you");
  await page.getByLabel("Message").fill("Finish the preview door and fix the wait bug, in parallel.");
  await page.getByRole("button", { name: "Send" }).click();
  await shoot(page, "workbench-said");

  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
  await shoot(page, "board");

  await page.goto("/atelier/workflows");
  await expect(page.getByRole("button", { name: "iterate-code" })).toBeVisible();
  await shoot(page, "workflows");

  await page.goto("/atelier/workflows/iterate-code");
  await expect(page.getByRole("heading", { level: 1, name: "iterate-code" })).toBeVisible();
  await shoot(page, "workflow-detail");

  await page.goto("/atelier/catalog");
  await expect(page.getByRole("heading", { level: 1, name: "Catalog" })).toBeVisible();
  await shoot(page, "catalog");

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

  // A run that its own contract stopped: the agent answers prose where the
  // node declared an object, so nothing writes a success and the run fails.
  const strictSchema = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: '{"type": "object"}'
  });
  expect([200, 201]).toContain(strictSchema.status());
  const strictHash = (await strictSchema.json()).schema_revision_hash as string;
  const failing = await chainOf(page, "publish-the-release", strictHash, ["implement", "review"]);
  const failedReference = await startRun(page, "demo/failed-contract", failing, agentHash);
  await runReaches(page, failedReference, "FAILED");
  await page.goto(`/atelier/runs/${failedReference}`);
  await expect(page.getByLabel("Where this run stands")).toContainText("Failed");
  await shoot(page, "run-failed");

  // A run that is still working: the delayed executor holds each node long
  // enough for the whole series to be photographed while it runs.
  const slowAgent = await agentOf(page, "shots-slow", "e2e-v3-slow", "delayed/v1");
  const running = await chainOf(page, "rebuild-the-index", schemaHash, [
    "gather", "compare", "rewrite", "verify"
  ]);
  const runningReference = await startRun(page, "demo/still-running", running, slowAgent);
  await page.goto(`/atelier/runs/${runningReference}`);
  await expect(page.getByRole("button", { name: /Working$/ })).toBeVisible({ timeout: 20_000 });
  await shoot(page, "run-running");

  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
  // Clay, blue, brick and quiet ink on one Board: the only place where the
  // four standings can be judged against each other.
  await shoot(page, "board-populated");

  await page.goto("/atelier/history");
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
  await shoot(page, "history");

  await page.goto("/atelier/project");
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
  await shoot(page, "project");
});
