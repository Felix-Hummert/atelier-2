import { expect, test, type Locator, type Page } from "@playwright/test";

import { historyPageCopy } from "../../src/lib/historyPageCopy";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { standingWords } from "../../src/lib/runState";
import { settingsPageCopy } from "../../src/lib/settingsPageCopy";
import { workbenchPageCopy } from "../../src/lib/workbenchPageCopy";
import { WORKSHOP_DESTINATION } from "../../src/lib/workshop";

/**
 * Click and glance budgets from mockup v8 §07. Each number is a door count on
 * the named frame, not an invented allowance. Catalog and the Run view stay
 * out of this slice (#698 D2 / #666). Queue-admit is named in §07 and has no
 * Workbench door yet — not asserted here. Settings' connect-a-source path is
 * a named deferral to #567 until that door exists, never a silent pass.
 *
 * Contract only: user-click count to the goal, and the named goal elements in
 * the viewport without scrolling at the picture's 390 and 1280 widths.
 */

/** Frame "Empty — only the ear" / "Loaded — the ear stays reachable without scrolling" (§02). */
const SEND_A_MESSAGE_CLICKS = 0;
const SEND_A_MESSAGE_GLANCES = 1;

/** Frame "Full — two open questions, two runs, the conversation with its cards" (§02). */
const ANSWER_A_DECISION_CLICKS = 1;
const ANSWER_A_DECISION_GLANCES = 1;
const DECISION_QUESTION = "Ship it, or hold it back?";

/** Frame "Full" / "Loaded — five runs, long conversation, the ear stays reachable without scrolling" (§02). */
const FIND_THE_RUNNING_RUN_CLICKS = 0;
const FIND_THE_RUNNING_RUN_GLANCES = 1;

/** Untitled populated History frame (§05). From another room: History + the line. */
const OPEN_FINISHED_RUN_FROM_HISTORY_CLICKS = 2;
const OPEN_FINISHED_RUN_FROM_HISTORY_GLANCES = 2;

/** Frame "Connect a source — the sheet · and the question before a disconnect" (§06). */
const CONNECT_A_SOURCE_CLICKS = 3;
const CONNECT_A_SOURCE_GLANCES = 2;
const CONNECT_A_SOURCE_DOOR = "Connect a source";

const VIEWPORTS = [
  { width: 1280, height: 900 },
  { width: 390, height: 844 }
] as const;

const API = "/atelier/api/v1";

type ClickBudget = {
  count: number;
  click(locator: Locator): Promise<void>;
};

function clickBudget(): ClickBudget {
  let count = 0;
  return {
    get count() {
      return count;
    },
    async click(locator) {
      count += 1;
      await locator.click();
    }
  };
}

async function resetToKnownStore(page: Page): Promise<void> {
  const reset = await page.request.post("/__e2e/recompose?reset=true");
  expect(reset.status()).toBe(202);
  const expectedGeneration = await reset.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
}

type ReconciliationFixtureRun = {
  public_run_reference: string;
  workflow_revision_hash: string;
  waiting: {
    type: "WAITING_RECONCILIATION";
    intent_state_version: number;
  };
};

type InputFixtureRun = {
  public_run_reference: string;
  workflow_revision_hash: string;
  waiting: {
    type: "WAITING_INPUT";
    node_id: string;
  };
};

async function retireReconciliationFixtures(page: Page): Promise<void> {
  const listed = await page.request.get(`${API}/runs?state=WAITING_RECONCILIATION&limit=50`);
  expect(listed.status()).toBe(200);
  const { items } = (await listed.json()) as { items: ReconciliationFixtureRun[] };
  expect(items).toHaveLength(2);

  for (const run of items) {
    expect(run.waiting.type).toBe("WAITING_RECONCILIATION");
    const retired = await page.request.post(
      `${API}/runs/${run.public_run_reference}/reconciliations`,
      {
        headers: { "content-type": "application/json" },
        data: {
          command_id: `reconcile-uiq-budget-${run.public_run_reference}`,
          expected_intent_state_version: run.waiting.intent_state_version,
          actor: "Playwright fixture isolation",
          evidence: "REQ-UIQ-02 stages its own Workbench shelf.",
          determination: { type: "operator_authoritative_absence" }
        }
      }
    );
    expect([200, 202]).toContain(retired.status());
  }

  await expect(async () => {
    const remaining = await page.request.get(`${API}/runs?state=WAITING_RECONCILIATION&limit=50`);
    expect(remaining.status()).toBe(200);
    expect(((await remaining.json()) as { items: unknown[] }).items).toHaveLength(0);
  }).toPass({ timeout: 20_000 });

  await expect(async () => {
    const states: string[] = [];
    for (const fixture of items) {
      const current = await page.request.get(`${API}/runs/${fixture.public_run_reference}`);
      expect(current.status()).toBe(200);
      const run = (await current.json()) as InputFixtureRun & { state: string };
      states.push(run.state);
      if (run.state === "WAITING_INPUT") {
        expect(run.waiting.type).toBe("WAITING_INPUT");
        const answered = await page.request.post(
          `${API}/runs/${run.public_run_reference}/answers`,
          {
            headers: { "content-type": "application/json" },
            data: {
              workflow_revision_hash: run.workflow_revision_hash,
              node_id: run.waiting.node_id,
              answer_base64: "MQ=="
            }
          }
        );
        expect([200, 202]).toContain(answered.status());
      }
    }
    expect(states).toEqual(["COMPLETED", "COMPLETED"]);
  }).toPass({ timeout: 20_000 });
}

async function publishSchema(page: Page, document: string): Promise<string> {
  const published = await page.request.post(`${API}/schema-revisions`, {
    headers: { "content-type": "application/json" },
    data: document
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

async function publishWaitWorkflow(page: Page, name: string, prompt: string): Promise<string> {
  const schemaHash = await publishSchema(page, '{"type":"boolean"}');
  const published = await page.request.post(`${API}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${name}`,
      "nodes:",
      "  - id: ship",
      "    type: wait",
      `    prompt: ${prompt}`,
      "    outputs:",
      "      - name: decision",
      "        schema:",
      "          ref: decision-schema",
      `          revision: ${schemaHash}`,
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  return (await published.json()).workflow_revision_hash as string;
}

async function startWaitRun(page: Page, runId: string, revisionHash: string): Promise<string> {
  const started = await page.request.post(`${API}/runs`, {
    data: {
      workflow_format_version: 3,
      run_id: runId,
      workflow_revision_hash: revisionHash,
      agent_bindings: [],
      orders: []
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;
  await expect(async () => {
    const read = await page.request.get(`${API}/runs/${reference}`);
    expect((await read.json()).state).toBe("WAITING_INPUT");
  }).toPass({ timeout: 20_000 });
  return reference;
}

async function startHeldRun(page: Page, name: string, runId: string): Promise<string> {
  const schemaHash = await publishSchema(page, "true");
  const published = await page.request.post(`${API}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${name}`,
      "nodes:",
      "  - id: work",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Stay in hand so the living row can be found.",
      "    outputs:",
      "      - name: result",
      "        schema:",
      "          ref: result-schema",
      `          revision: ${schemaHash}`,
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;
  const auth = await page.request.post(`${API}/auth-profile-revisions`, {
    data: {
      profile_id: "uiq-budget-held",
      revision_number: 1,
      provider_id: "e2e-v3-held",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post(`${API}/agent-configuration-revisions`, {
    data: {
      model: "uiq-budget-held-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "held/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(page, "e2e-v3-held", "uiq-budget-held-model", agentHash);
  const started = await page.request.post(`${API}/runs`, {
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
    const read = await page.request.get(`${API}/runs/${reference}`);
    expect((await read.json()).state).toBe("STARTED");
  }).toPass({ timeout: 20_000 });
  return reference;
}

async function startFinishedRun(page: Page, name: string, runId: string): Promise<string> {
  const schemaHash = await publishSchema(page, "true");
  const published = await page.request.post(`${API}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${name}`,
      "nodes:",
      "  - id: work",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Finish so History can open the result.",
      "    outputs:",
      "      - name: result",
      "        schema:",
      "          ref: result-schema",
      `          revision: ${schemaHash}`,
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;
  const auth = await page.request.post(`${API}/auth-profile-revisions`, {
    data: {
      profile_id: "uiq-budget-done",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect([200, 201]).toContain(auth.status());
  const configuration = await page.request.post(`${API}/agent-configuration-revisions`, {
    data: {
      model: "uiq-budget-done-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect([200, 201]).toContain(configuration.status());
  const agentHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await publishCheckedRegistryEntry(page, "e2e-v3", "uiq-budget-done-model", agentHash);
  const started = await page.request.post(`${API}/runs`, {
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
    const read = await page.request.get(`${API}/runs/${reference}`);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 20_000 });
  return reference;
}

async function openWorkbench(page: Page): Promise<void> {
  await page.goto("/atelier/chat");
  await expect(page.getByRole("heading", { name: workbenchPageCopy.title })).toBeVisible();
  await page.locator(".workshop-stage").evaluate((element) => {
    element.scrollTop = 0;
  });
}

function rail(page: Page) {
  return page.getByRole("navigation", { name: "Workshop" });
}

test("proves(core-tasks-meet-named-click-and-glance-budgets): Workbench and History core tasks stay inside mockup v8 click and glance budgets at 390 and 1280", async ({
  page
}, testInfo) => {
  test.setTimeout(180_000);
  const suffix = testInfo.repeatEachIndex;
  const runningName = `uiq-running-${suffix}`;
  const finishedName = `uiq-finished-${suffix}`;
  const waitingName = `uiq-waiting-${suffix}`;

  await resetToKnownStore(page);
  await retireReconciliationFixtures(page);
  await startFinishedRun(page, finishedName, `uiq/finished-${suffix}`);
  await startHeldRun(page, runningName, `uiq/running-${suffix}`);
  const waitRevision = await publishWaitWorkflow(page, waitingName, DECISION_QUESTION);

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await startWaitRun(page, `uiq/waiting-${suffix}-${viewport.width}`, waitRevision);
    await openWorkbench(page);

    const composer = page.getByLabel(workbenchPageCopy.composerLabel);
    const sendGlances = [composer];
    for (const glance of sendGlances) {
      await expect(glance, `send-a-message glance at ${viewport.width}`).toBeInViewport();
    }
    expect(sendGlances.length).toBe(SEND_A_MESSAGE_GLANCES);
    const send = clickBudget();
    const spoken = `budget probe ${viewport.width}`;
    await composer.fill(spoken);
    await composer.press("Enter");
    await expect(page.getByText(spoken)).toBeVisible();
    expect(send.count, `send-a-message clicks at ${viewport.width}`).toBeLessThanOrEqual(
      SEND_A_MESSAGE_CLICKS
    );

    const findRunning = clickBudget();
    const runningRow = page.getByRole("link", { name: new RegExp(runningName) });
    await expect(runningRow).toBeVisible({ timeout: 20_000 });
    const runningGlances = [runningRow];
    for (const glance of runningGlances) {
      await expect(glance, `find-the-running-run glance at ${viewport.width}`).toBeInViewport();
    }
    expect(runningGlances.length).toBe(FIND_THE_RUNNING_RUN_GLANCES);
    expect(findRunning.count, `find-the-running-run clicks at ${viewport.width}`).toBeLessThanOrEqual(
      FIND_THE_RUNNING_RUN_CLICKS
    );

    const yes = page.getByRole("button", { name: runPageCopy.answerYes });
    await expect(page.getByRole("heading", { name: DECISION_QUESTION })).toBeVisible({
      timeout: 20_000
    });
    const decisionGlances = [yes];
    for (const glance of decisionGlances) {
      await expect(glance, `answer-a-decision glance at ${viewport.width}`).toBeInViewport();
    }
    expect(decisionGlances.length).toBe(ANSWER_A_DECISION_GLANCES);
    const answer = clickBudget();
    await answer.click(yes);
    await expect(yes).toHaveCount(0, { timeout: 20_000 });
    expect(answer.count, `answer-a-decision clicks at ${viewport.width}`).toBeLessThanOrEqual(
      ANSWER_A_DECISION_CLICKS
    );
  }

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await openWorkbench(page);

    const historyPath = clickBudget();
    await historyPath.click(rail(page).getByRole("link", { name: WORKSHOP_DESTINATION.history.label }));
    await expect(page.getByRole("heading", { name: historyPageCopy.title })).toBeVisible();
    const finishedRow = page.getByRole("link", { name: new RegExp(finishedName) });
    await expect(finishedRow).toBeVisible();
    await expect(finishedRow, `history line glance at ${viewport.width}`).toBeInViewport();
    let historyGlances = 1;
    await historyPath.click(finishedRow);
    await expect(page).toHaveURL(/\/atelier\/runs\/run1\./);
    const resultSentence = page.getByLabel("Where this run stands");
    await expect(resultSentence).toContainText(standingWords.done);
    await expect(resultSentence, `history sentence glance at ${viewport.width}`).toBeInViewport();
    historyGlances += 1;
    expect(historyGlances).toBe(OPEN_FINISHED_RUN_FROM_HISTORY_GLANCES);
    expect(historyPath.count).toBeLessThanOrEqual(OPEN_FINISHED_RUN_FROM_HISTORY_CLICKS);
  }
});

test("Settings connect-a-source stays inside mockup v8 click and glance budgets at 390 and 1280", async ({
  page
}) => {
  test.setTimeout(60_000);
  await resetToKnownStore(page);

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await openWorkbench(page);
    const connectPath = clickBudget();
    await connectPath.click(
      rail(page).getByRole("link", { name: WORKSHOP_DESTINATION.settings.label })
    );
    const sources = page.getByRole("heading", { name: settingsPageCopy.sourcesTitle });
    await expect(sources).toBeVisible();
    await expect(sources, `settings room glance at ${viewport.width}`).toBeInViewport();
    const connectDoor = page.getByRole("button", { name: CONNECT_A_SOURCE_DOOR });
    test.fixme(
      (await connectDoor.count()) === 0,
      "Settings has no Connect-a-source door yet — owner #567 (mockup v8 §06/§07)"
    );
    await expect(connectDoor, `connect-a-source room glance at ${viewport.width}`).toBeInViewport();
    let connectGlances = 1;
    await connectPath.click(connectDoor);
    const sheet = page.getByRole("heading", { name: CONNECT_A_SOURCE_DOOR });
    await expect(sheet, `connect-a-source sheet glance at ${viewport.width}`).toBeInViewport();
    connectGlances += 1;
    expect(connectGlances).toBe(CONNECT_A_SOURCE_GLANCES);
    await connectPath.click(page.getByRole("button", { name: "Connect", exact: true }));
    expect(connectPath.count).toBeLessThanOrEqual(CONNECT_A_SOURCE_CLICKS);
  }
});
