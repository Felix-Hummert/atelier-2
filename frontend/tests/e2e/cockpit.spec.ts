import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page, type Route } from "@playwright/test";

import { NAMED_AGENT_CHOICE_STORAGE_KEY } from "../../src/lib/namedAgentChoice";
import { THE_ONE_PROJECT } from "../../src/lib/project";

const foundReference = "run1.Zm91bmQtcnVu";
const absentReference = "run1.YWJzZW50LXJ1bg";

// Every executable V3 agent node declares exactly one output and the schema it
// must satisfy: that is `single-json-output/v1`, the one output shape a run
// enforces. Where a test is about something else, it pins the schema that admits
// any JSON value, so the node's contract says no more than the shape requires.
async function publishSchema(page: Page, document: string): Promise<string> {
  const published = await page.request.post("/atelier/api/v1/schema-revisions", {
    headers: { "content-type": "application/json" },
    data: document
  });
  expect([200, 201]).toContain(published.status());
  return (await published.json()).schema_revision_hash as string;
}

const anyJsonSchema = (page: Page): Promise<string> => publishSchema(page, "true");

function declaredOutput(schemaHash: string, name = "result"): string[] {
  return [
    "    outputs:",
    `      - name: ${name}`,
    "        schema:",
    `          ref: ${name}-schema`,
    `          revision: ${schemaHash}`
  ];
}

test("the target-UI shell names today's doors and does not fake the rest", async ({ page }) => {
  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();

  const rail = page.getByRole("navigation", { name: "Workshop" });
  await expect(rail.getByText("atelier", { exact: true })).toBeVisible();
  await expect(rail.getByRole("link", { name: "Board" })).toBeVisible();
  await expect(rail.getByRole("link", { name: "Workflows" })).toBeVisible();
  await expect(rail.getByRole("link", { name: "History" })).toBeVisible();
  await expect(rail.getByRole("link", { name: "Chat" })).toHaveCount(0);
  await expect(rail.getByText("Chat", { exact: true })).toBeVisible();
  await expect(rail.locator("[title*='#7']")).toBeVisible();
  await expect(rail.getByText(THE_ONE_PROJECT, { exact: true })).toBeVisible();
  await expect(rail.getByText("switch project")).toBeVisible();
  await expect(rail.getByText("Settings", { exact: true })).toBeVisible();
  await expect(rail.getByText("Profile", { exact: true })).toBeVisible();
  await expect(rail.getByText("(later)", { exact: true })).toHaveCount(2);

  await rail.getByRole("link", { name: "History" }).click();
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/history$/);

  await rail.getByRole("link", { name: "Workflows" }).click();
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/workflows$/);

  await rail.getByRole("link", { name: "Board" }).click();
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier$/);

  await page.screenshot({ path: "test-results/shell-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("navigation", { name: "Workshop" })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/shell-390x844.png", fullPage: true });
});

test("proves(core-surfaces-support-one-complete-keyboard-journey): publishes, binds, and starts one visible V2 Agent", async ({ page }) => {
  await page.goto("/atelier/new");
  await page.getByLabel("Publish YAML").check();
  await page.getByLabel("Exact workflow YAML").fill("format_version: 2\nstart: build\nnodes:\n  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}\n  - {id: build, type: agent, role: builder, job: prove-heartbeat-84, next: done}\n");
  await page.getByRole("button", { name: "Review publication" }).click();
  await expect(page.getByRole("dialog", { name: "Publish this exact workflow?" })).toBeVisible();
  const publishedResponse = page.waitForResponse(
    (response) => response.url().endsWith("/workflow-revisions") && response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  const published = await publishedResponse;
  expect(published.status()).toBe(201);
  const revision = await published.json();
  expect(revision.workflow_revision_hash).toMatch(/^[0-9a-f]{64}$/);
  await page.getByLabel("Saved workflow").check();
  await expect(
    page.getByRole("article", { name: revision.workflow_revision_hash, exact: true })
  ).toBeVisible();
  const auth = await page.request.post("/atelier/api/v1/auth-profile-revisions", {
    data: { profile_id: "local", revision_number: 1, provider_id: "e2e", auth_mode: "subscription" }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post("/atelier/api/v1/agent-configuration-revisions", {
    data: {
      model: "test-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "blocking/v1"
    }
  });
  expect(configuration.status()).toBe(201);
  const configurationHash = (await configuration.json()).agent_configuration_revision_hash as string;
  await page.evaluate(({ key, hash }) => {
    localStorage.setItem(key, JSON.stringify({ builder: hash }));
  }, { key: NAMED_AGENT_CHOICE_STORAGE_KEY, hash: configurationHash });

  // Starting a run is Workflows' own door now (#532: Board carries no Start of
  // any kind). The V2 workflow this journey starts declares no name of its
  // own -- only a V3 document can (`boardRows.ts`'s honest run-id fallback
  // says the same) -- so a second, minimal named V3 workflow exists purely as
  // the keyboard vehicle into New Run; the run this journey proves out is
  // still chosen by its own hash below, unrelated to this vehicle.
  const doorSchemaHash = await anyJsonSchema(page);
  const doorName = "Keyboard journey door";
  const door = await page.request.post("/atelier/api/v1/workflow-revisions", {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${doorName}`,
      "nodes:",
      "  - id: ask",
      "    type: wait",
      "    prompt: Unused -- this workflow only opens the door to New Run.",
      ...declaredOutput(doorSchemaHash, "answer"),
      ""
    ].join("\n")
  });
  expect(door.status()).toBe(201);

  await page.goto("/atelier");
  await page.evaluate(() => {
    const observed: string[] = [];
    document.addEventListener("focusin", (event) => {
      if (event.target !== document.querySelector("main.workshop-stage")) return;
      const marker = ["#new-title", "#board-title", ".trail-here"]
        .map((selector) => document.querySelector(selector))
        .find((candidate) => candidate !== null);
      if (marker instanceof HTMLElement) observed.push(marker.id || marker.className);
    });
    Object.assign(window, { observedMainMarkers: observed });
  });
  const stage = page.getByRole("main");
  // The harness seeds two waiting runs at boot, so Board is never in its
  // empty state here.
  const rail = page.getByRole("navigation", { name: "Workshop" });
  const workflowsLink = rail.getByRole("link", { name: "Workflows" });
  for (let tab = 0; tab < 8 && !(await workflowsLink.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(workflowsLink).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(stage).toBeFocused();

  const doorCard = page.getByRole("button", { name: new RegExp(doorName) });
  await expect(doorCard).toBeVisible();
  for (let tab = 0; tab < 8 && !(await doorCard.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(doorCard).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(stage).toBeFocused();

  const startDoor = page.getByRole("button", { name: "Start", exact: true });
  for (let tab = 0; tab < 8 && !(await startDoor.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(startDoor).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(stage).toBeFocused();
  const savedRevision = page
    .getByRole("article", { name: revision.workflow_revision_hash, exact: true })
    .getByRole("radio");
  await expect(savedRevision).toBeVisible();
  const saved = page.getByRole("radio", { name: "Saved workflow" });
  for (let tab = 0; tab < 8 && !(await saved.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(saved).toBeFocused();
  await expect(saved).toBeChecked();
  for (let tab = 0; tab < 8 && !(await page.evaluate(() => document.activeElement?.getAttribute("name") === "saved-revision")); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(savedRevision).toBeFocused();
  await page.keyboard.press("Space");
  await expect(savedRevision).toBeChecked();
  await expect(page.getByLabel("Agent for builder")).toHaveValue(configurationHash);
  const start = page.getByRole("button", { name: "Start" });
  for (let tab = 0; tab < 12 && !(await start.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(start).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/atelier\/runs\//);
  await expect(stage).toBeFocused();
  const runPath = new URL(page.url()).pathname;
  const runRead = new URL(
    `/atelier/api/v1/runs/${runPath.slice("/atelier/runs/".length)}`,
    page.url()
  ).toString();

  const working = page.getByRole("article", { name: "build — Working" });
  await expect(working).toContainText("e2e · test-model");
  await expect(working).toContainText("Subscription · blocking/v1");
  await expect(working).toHaveAttribute("data-live", "true");
  await expect(page.getByText("Process log stays in the lease.")).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  // The header carries no manual refresh (#506): the live stream, already
  // open, is the one honest freshness model for a run that is neither
  // stopped nor failed. The fake process this run drives still needs the
  // durable run read more than once before it lets go of the attempt it is
  // holding open (a fixture concern, not a UI one), so the test reads it
  // out of band here instead of through a page action that no longer exists.
  await expect(page.getByRole("button", { name: "Refresh" })).toHaveCount(0);
  const secondRead = await page.request.get(runRead);
  expect(secondRead.status()).toBe(200);
  const thirdRead = await page.request.get(runRead);
  expect(thirdRead.status()).toBe(200);
  const completed = page.getByRole("article", { name: "build — Done" });
  await expect(completed).toBeVisible({ timeout: 8_000 });
  await expect(completed).toContainText("Provider terminal evidence:");
  await expect(completed).toContainText("Grüße 東京 — durable agent output remains readable after completion.");
  await expect(completed).toContainText("1528 bytes");
  await expect(completed).toContainText("Verified");
  await expect(page.getByTestId("run-state")).toHaveText("completed");
  await expect(page.locator(".connection")).toHaveText(/Complete/);
  const terminalHash = page.getByRole("group", { name: "Terminal hash" });
  const terminalHashButton = terminalHash.getByRole("button", { name: "Terminal hash" });
  await expect(terminalHash).toBeVisible();
  for (let tab = 0; tab < 12 && !(await terminalHashButton.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(terminalHashButton).toBeFocused();
  await page.keyboard.press("Space");
  await expect(terminalHash.locator("code")).toHaveText(/^[0-9a-f]{64}$/);
  await page.setViewportSize({ width: 1280, height: 900 });
  const eventLog = page.locator("details.event-log");
  await expect(eventLog).toHaveJSProperty("open", false);
  const events = eventLog.locator("summary");
  for (let tab = 0; tab < 20 && !(await events.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(events).toBeFocused();
  await page.keyboard.press("Space");
  await expect(eventLog).toHaveJSProperty("open", true);
  const durableEvents = eventLog.getByRole("listitem");
  await expect(durableEvents).toHaveCount(2);
  await expect(eventLog.getByRole("group", { name: "AGENT COMPLETED #1" })).toBeVisible();
  await expect(eventLog.getByRole("group", { name: "SUBWORKFLOW COMPLETED #2" })).toBeVisible();
  const agentEvidence = page.getByRole("region", { name: "Event evidence #1" });
  await expect(agentEvidence).toHaveAttribute("tabindex", "0");
  await expect(agentEvidence.locator("pre")).toContainText('"sequence":1');
  await expect(agentEvidence.locator("pre")).toContainText('"event":"AGENT_COMPLETED"');
  await expect(agentEvidence.locator("pre")).toContainText(
    '"output_hash":"f772309569117f3945e1296d0a524b1e3a100bd0697699ad8394d01a26ea2555"'
  );
  for (let tab = 0; tab < 12 && !(await agentEvidence.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(agentEvidence).toBeFocused();
  await page.keyboard.press("Home");
  await expect.poll(() => agentEvidence.evaluate((element) => element.scrollTop)).toBe(0);
  const desktopScrollTop = await agentEvidence.evaluate((element) => element.scrollTop);
  await page.keyboard.press("PageDown");
  await expect.poll(() => agentEvidence.evaluate((element) => element.scrollTop)).toBeGreaterThan(desktopScrollTop);
  await expectVisibleFocus(agentEvidence);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({ path: "test-results/v2-keyboard-journey-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.keyboard.press("Tab");
  for (let tab = 0; tab < 40 && !(await agentEvidence.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(agentEvidence).toBeFocused();
  await page.keyboard.press("Home");
  await expect.poll(() => agentEvidence.evaluate((element) => element.scrollTop)).toBe(0);
  const mobileScrollTop = await agentEvidence.evaluate((element) => element.scrollTop);
  await page.keyboard.press("PageDown");
  await expect.poll(() => agentEvidence.evaluate((element) => element.scrollTop)).toBeGreaterThan(mobileScrollTop);
  await expectVisibleFocus(agentEvidence);
  await assertNoSeriousAccessibilityFindings(page);
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/v2-keyboard-journey-390x844.png", fullPage: true });
  const board = page.getByRole("navigation", { name: "Workshop" }).getByRole("link", { name: "Board" });
  for (let tab = 0; tab < 40 && !(await board.evaluate((element) => element === document.activeElement)); tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(board).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(stage).toBeFocused();
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as unknown as { observedMainMarkers: string[] }).observedMainMarkers)).toEqual([
    // The door workflow's own detail page carries the same breadcrumb
    // component a run does, hence the leading "trail-here" this journey now
    // passes through on its way from Board via Workflows into New Run.
    "trail-here", "new-title", "trail-here", "board-title"
  ]);
});


/**
 * A click never asks the server for a page, so the project level looked right
 * while a reload of it answered 404. This walks the way an operator arrives from
 * outside — the pasted link — and then reloads the level he is standing on.
 */
test("opens the project level from a cold link and survives a reload", async ({ page }) => {
  await page.goto("/atelier/project");
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/project$/);
});

test("proves(the-studio-preserves-confirmed-truth-and-retries-only-its-failed-read): Board recovers one retained five-list-plus-catalog read", async ({ page }) => {
  const runListPath = "/atelier/api/v1/runs";
  const catalogPath = "/atelier/api/v1/workflow-revisions";
  const expectedStates = [
    "COMPLETED",
    "FAILED",
    "STARTED",
    "WAITING_INPUT",
    "WAITING_RECONCILIATION"
  ];
  let readsFail = true;
  const observed: Array<{ method: string; path: string; state: string | null }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/atelier/api/v1")) {
      observed.push({
        method: request.method(),
        path: url.pathname,
        state: url.searchParams.get("state")
      });
    }
  });
  await page.route("**/atelier/api/v1/runs?*", async (route) => {
    if (readsFail) await route.abort("failed");
    else await route.continue();
  });

  const expectOnlyBoardRead = (): void => {
    const runRequests = observed.filter(({ path }) => path === runListPath);
    const catalogRequests = observed.filter(({ path }) => path === catalogPath);
    expect(observed).toHaveLength(runRequests.length + catalogRequests.length);
    expect(runRequests.every(({ method }) => method === "GET")).toBe(true);
    expect(runRequests.map(({ state }) => state).sort()).toEqual(expectedStates);
    expect(catalogRequests).toHaveLength(1);
    expect(catalogRequests[0]?.method).toBe("GET");
  };

  await page.goto("/atelier");
  await expect(page.getByText("Board runs unavailable")).toBeVisible();
  await expect(page.getByText(/Failed to fetch/)).toHaveCount(0);
  const retry = page.getByRole("button", { name: "Retry board runs" });
  await expect(retry).toHaveCount(1);
  const boardUrl = page.url();

  observed.length = 0;
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Board runs unavailable")).toBeVisible();
  await expect(retry).toBeFocused();
  expectOnlyBoardRead();
  expect(page.url()).toBe(boardUrl);

  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(retry).toBeFocused();
  await expectVisibleFocus(retry);
  await assertNoSeriousAccessibilityFindings(page);
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({
    path: "test-results/read-recovery-studio-grayscale-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/read-recovery-studio-grayscale-390x844.png",
    fullPage: true
  });
  await page.locator("style").last().evaluate((element) => element.remove());

  readsFail = false;
  observed.length = 0;
  await page.getByRole("button", { name: "Retry board runs" }).click();
  const board = page.locator(".board-page");
  await expect(page.getByText("Board runs unavailable")).toHaveCount(0);
  await expect(board).toBeVisible();
  // One freshness model, once confirmed: no Refresh or Retry control remains
  // beside the live indicator (#532) -- the redundant permanent control this
  // lane removes.
  await expect(page.getByRole("button", { name: /board runs/ })).toHaveCount(0);
  expectOnlyBoardRead();
  expect(page.url()).toBe(boardUrl);
});

test("proves(the-project-preserves-confirmed-truth-and-retries-only-its-failed-read): Project recovers one atomic run-and-name read", async ({ page }) => {
  const runListPath = "/atelier/api/v1/runs";
  const oldHash = "1".repeat(64);
  const newHash = "2".repeat(64);
  const oldReference = "run1.cHJvamVjdC1vbGQ";
  const newReference = "run1.cHJvamVjdC1uZXc";
  const run = (runId: string, reference: string, hash: string, startedAt: string) => ({
    workflow_format_version: 3,
    run_id: runId,
    public_run_reference: reference,
    workflow_revision_hash: hash,
    agent_binding_set_hash: "3".repeat(64),
    run_configuration_revision_hash: "4".repeat(64),
    agent_bindings: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "review",
    node_rail: [{ node_id: "review", state: "working", attempt: null }],
    terminal_hash: null,
    latest_event_cursor: null,
    started_at: startedAt,
    ended_at: null
  });
  const revision = (hash: string, name: string) => ({
    workflow_revision_hash: hash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["builder"],
      orders: [],
      node_previews: [
        {
          id: "review",
          kind: "agent",
          role: "builder",
          instruction_start: "Review the result.",
          depends_on: []
        }
      ],
      loops: [],
      name,
      description: null
    }
  });
  const oldRun = run(
    "confirmed project run",
    oldReference,
    oldHash,
    "2026-08-20T12:00:00Z"
  );
  const newRun = run(
    "new project run",
    newReference,
    newHash,
    "2026-08-20T13:00:00Z"
  );
  // round tracks each attempt at the run list: 1 fails at transport, 2
  // succeeds at the run list but fails the joint name read for one hash
  // (still hidden, still no confirm), 3 confirms both rows atomically. No
  // step needs a manual refresh: the read is confirmed only once, and only
  // the one accessible Retry ever repeats it (#532 removes the permanent
  // control every one of these five surfaces used to carry alongside it).
  let round = 0;
  const observed: Array<{ method: string; path: string }> = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/atelier/api/v1")) {
      observed.push({ method: request.method(), path });
    }
  });
  await page.route("**/atelier/api/v1/runs*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path !== runListPath) {
      await route.continue();
      return;
    }
    round += 1;
    if (round === 1) {
      await route.abort("failed");
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [oldRun, newRun], next_after: null })
    });
  });
  await page.route("**/atelier/api/v1/workflow-revisions/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const hash = path.slice(path.lastIndexOf("/") + 1);
    if (hash !== oldHash && hash !== newHash) {
      await route.continue();
      return;
    }
    if (round === 2 && hash === newHash) {
      await route.abort("failed");
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(revision(hash, hash === oldHash ? "Confirmed workflow" : "New workflow"))
    });
  });

  const revisionPath = (hash: string): string =>
    `/atelier/api/v1/workflow-revisions/${hash}`;
  const expectOnlyProjectRead = (paths: string[]): void => {
    expect(observed.every(({ method }) => method === "GET")).toBe(true);
    expect(observed.map(({ path }) => path).sort()).toEqual([...paths].sort());
  };

  await page.goto("/atelier/project");
  await expect(page.getByText("Project runs unavailable")).toBeVisible();
  await expect(page.getByText(/Failed to fetch/)).toHaveCount(0);
  const retry = page.getByRole("button", { name: "Retry project runs" });
  await expect(retry).toHaveCount(1);
  const projectUrl = page.url();

  // Round 2: the run list itself now answers, but the joint name read for
  // one hash fails -- the read still confirms nothing (atomicity), and the
  // transport detail still stays hidden.
  observed.length = 0;
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Project runs unavailable")).toBeVisible();
  await expect(retry).toBeFocused();
  await expect(page.getByRole("link", { name: /confirmed project run|new project run/ })).toHaveCount(0);
  expectOnlyProjectRead([runListPath, revisionPath(oldHash), revisionPath(newHash)]);
  expect(page.url()).toBe(projectUrl);

  // ReadState.svelte's control mounts only in the failed state (#514's
  // pattern): the operator's own retry that fails again re-mounts a new
  // Retry and returns focus to it, so the same locator keeps resolving.
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(retry).toBeFocused();
  await expectVisibleFocus(retry);
  await assertNoSeriousAccessibilityFindings(page);
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({
    path: "test-results/read-recovery-project-grayscale-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/read-recovery-project-grayscale-390x844.png",
    fullPage: true
  });
  await page.locator("style").last().evaluate((element) => element.remove());

  // Round 3: both the run list and every name resolve -- the whole read
  // confirms together.
  observed.length = 0;
  await retry.click();
  await expect(page.getByText("Project runs unavailable")).toHaveCount(0);
  const confirmedRow = page.getByRole("link", { name: /confirmed project run/ });
  await expect(confirmedRow).toContainText("Confirmed workflow");
  await expect(page.getByRole("link", { name: /new project run/ })).toContainText("New workflow");
  // One freshness model, once confirmed: no manual refresh remains.
  await expect(page.getByRole("button", { name: /project runs/ })).toHaveCount(0);
  expectOnlyProjectRead([runListPath, revisionPath(oldHash), revisionPath(newHash)]);
  expect(page.url()).toBe(projectUrl);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({
    path: "test-results/read-recovery-project-desktop.png",
    fullPage: true
  });
});

test("proves(new-run-preserves-workflow-truth-and-retries-only-the-workflow-read): New Run recovers one atomic workflow-and-catalog read", async ({ page }) => {
  const workflowListPath = "/atelier/api/v1/workflow-revisions";
  const agentListPath = "/atelier/api/v1/agent-configuration-revisions";
  const retainedName = "retained-workflow";
  const newName = "new-workflow";
  const confirmedHash = "1".repeat(64);
  const refreshedHash = "2".repeat(64);
  const newHash = "3".repeat(64);
  const absentHeadHash = "6".repeat(64);
  const summary = (hash: string, name: string | null, description: string | null) => ({
    workflow_revision_hash: hash,
    workflow_format_version: name === null ? 2 : 3,
    executable: true,
    not_executable_reason: null,
    name,
    description
  });
  const catalogHead = (name: string, hash: string, revisionNumber: number) => ({
    display_name: name,
    lineage_id: "5".repeat(64),
    workflow_revision_hash: hash,
    revision_number: revisionNumber
  });
  const listTarget = (after?: string): string =>
    after === undefined
      ? `${workflowListPath}?limit=50&view=described`
      : `${workflowListPath}?limit=50&view=described&after=${after}`;
  const catalogTarget = (name: string): string =>
    `${workflowListPath}/by-name/${name}`;
  // round tracks each attempt at the workflow list: 1 fails at transport, 2
  // succeeds at the list but fails the joint catalog-head read for one name
  // (an admitted head absent from the same listing -- still no confirm), 3
  // confirms every row atomically. No step needs a manual refresh: the read
  // is confirmed only once, and only the one accessible Retry ever repeats
  // it (#532 removes the permanent control every one of these five surfaces
  // used to carry alongside it).
  let round = 0;
  const observed: Array<{ method: string; target: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/atelier/api/v1")) {
      observed.push({ method: request.method(), target: `${url.pathname}${url.search}` });
    }
  });
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], next_after_revision_hash: null })
    });
  });
  await page.route("**/atelier/api/v1/workflow-revisions?*", async (route) => {
    const url = new URL(route.request().url());
    const after = url.searchParams.get("after");
    if (after === null) round += 1;
    if (round === 1) {
      await route.abort("failed");
      return;
    }
    if (after === null) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            summary(confirmedHash, retainedName, "The confirmed catalog head."),
            summary(refreshedHash, retainedName, "The refreshed catalog head.")
          ],
          next_after_revision_hash: refreshedHash
        })
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [summary(newHash, newName, "A newly confirmed catalog line.")],
        next_after_revision_hash: null
      })
    });
  });
  await page.route("**/atelier/api/v1/workflow-revisions/by-name/*", async (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    const hash = name === retainedName
      ? refreshedHash
      : round === 2 ? absentHeadHash : newHash;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(catalogHead(name, hash, hash === confirmedHash ? 1 : 2))
    });
  });

  const expectOnlyWorkflowRead = (targets: string[]): void => {
    expect(observed.every(({ method }) => method === "GET")).toBe(true);
    expect(observed.map(({ target }) => target).sort()).toEqual([...targets].sort());
  };

  await page.goto("/atelier/new");
  await expect(page.getByText("Saved workflows unavailable")).toBeVisible();
  await expect(page.getByText(/Failed to fetch|private/i)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Retry saved workflows" })).toHaveCount(1);
  expect(observed.map(({ target }) => target).sort()).toEqual([
    `${agentListPath}?limit=50`,
    listTarget()
  ].sort());
  const retry = page.getByRole("button", { name: "Retry saved workflows" });
  const newRunUrl = page.url();

  observed.length = 0;
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Saved workflows unavailable")).toBeVisible();
  await expect(retry).toBeFocused();
  await expect(page.getByRole("article", { name: retainedName })).toHaveCount(0);
  await expect(page.getByRole("article", { name: newName })).toHaveCount(0);
  expectOnlyWorkflowRead([
    listTarget(),
    listTarget(refreshedHash),
    catalogTarget(retainedName),
    catalogTarget(newName)
  ]);
  expect(page.url()).toBe(newRunUrl);

  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(retry).toBeFocused();
  await expectVisibleFocus(retry);
  await assertNoSeriousAccessibilityFindings(page);
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({
    path: "test-results/read-recovery-new-run-grayscale-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/read-recovery-new-run-grayscale-390x844.png",
    fullPage: true
  });
  await page.locator("style").last().evaluate((element) => element.remove());
  await page.setViewportSize({ width: 1280, height: 900 });

  observed.length = 0;
  await retry.click();
  await expect(page.getByText("Saved workflows unavailable")).toHaveCount(0);
  const retained = page.getByRole("article", { name: retainedName });
  await expect(retained).toContainText("The refreshed catalog head.");
  await expect(retained).toHaveAttribute("data-catalog-form", "ready");
  const added = page.getByRole("article", { name: newName });
  await expect(added).toContainText("A newly confirmed catalog line.");
  await expect(added).toHaveAttribute("data-catalog-form", "ready");
  // One freshness model, once confirmed: no manual refresh remains.
  await expect(page.getByRole("button", { name: /saved workflows/ })).toHaveCount(0);
  expectOnlyWorkflowRead([
    listTarget(),
    listTarget(refreshedHash),
    catalogTarget(retainedName),
    catalogTarget(newName)
  ]);
  expect(page.url()).toBe(newRunUrl);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({
    path: "test-results/read-recovery-new-run-desktop.png",
    fullPage: true
  });
});

test("proves(new-run-preserves-agent-and-draft-truth-and-retries-only-the-agent-read): New Run retains one complete agent read and its draft", async ({ page }) => {
  const agentListPath = "/atelier/api/v1/agent-configuration-revisions";
  const workflowHash = "7".repeat(64);
  const firstHash = "8".repeat(64);
  const chosenHash = "9".repeat(64);
  const addedHash = "a".repeat(64);
  const workflowName = "Agent recovery proof";
  const agent = (hash: string, provider: string, model: string) => ({
    model,
    auth_profile_revision_hash: "b".repeat(64),
    executor_revision: `${provider}/v1`,
    provider_id: provider,
    auth_mode: "subscription",
    requested_capability: "headless",
    agent_configuration_revision_hash: hash,
    startable: true,
    not_startable_reason: null
  });
  const first = agent(firstHash, "anthropic", "sonnet");
  const chosen = agent(chosenHash, "openai", "codex");
  const added = agent(addedHash, "google", "gemini");
  const agentTarget = (after?: string): string =>
    after === undefined
      ? `${agentListPath}?limit=50`
      : `${agentListPath}?limit=50&after_revision_hash=${after}`;
  // agentRound tracks each attempt at the agent list: 1 fails at transport,
  // 2 reads its first page but fails the second (a partial page still
  // confirms nothing), 3 confirms every page together. No step needs a
  // manual refresh: the read is confirmed only once, and only the one
  // accessible Retry ever repeats it (#532 removes the permanent control
  // every one of these five surfaces used to carry alongside it).
  let agentRound = 0;
  const observed: Array<{ method: string; target: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/atelier/api/v1")) {
      observed.push({ method: request.method(), target: `${url.pathname}${url.search}` });
    }
  });
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => {
    const after = new URL(route.request().url()).searchParams.get("after_revision_hash");
    if (after === null) agentRound += 1;
    if (agentRound === 1) {
      await route.abort("failed");
      return;
    }
    if (agentRound === 2 && after !== null) {
      await route.abort("failed");
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(after === null
        ? { items: [first], next_after_revision_hash: firstHash }
        : { items: [chosen, added], next_after_revision_hash: null })
    });
  });
  await page.route("**/atelier/api/v1/workflow-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{
          workflow_revision_hash: workflowHash,
          workflow_format_version: 3,
          executable: true,
          not_executable_reason: null,
          name: workflowName,
          description: "Choose an agent without losing the draft."
        }],
        next_after_revision_hash: null
      })
    });
  });
  await page.route("**/atelier/api/v1/workflow-revisions/by-name/*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        display_name: workflowName,
        lineage_id: "c".repeat(64),
        workflow_revision_hash: workflowHash,
        revision_number: 1
      })
    });
  });
  await page.route(`**/atelier/api/v1/workflow-revisions/${workflowHash}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workflow_revision_hash: workflowHash,
        document_base64: "YQ==",
        graph: {
          workflow_format_version: 3,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: ["builder"],
          orders: [],
          node_previews: [{
            id: "implement",
            kind: "agent",
            role: "builder",
            instruction_start: "Build the candidate.",
            depends_on: []
          }],
          loops: [],
          name: workflowName,
          description: "Choose an agent without losing the draft."
        }
      })
    });
  });
  const expectOnlyAgentRead = (targets: string[]): void => {
    expect(observed.every(({ method }) => method === "GET")).toBe(true);
    expect(observed.map(({ target }) => target).sort()).toEqual([...targets].sort());
  };

  await page.goto("/atelier/new");
  const workflow = page.getByRole("radio", { name: new RegExp(workflowName) });
  await workflow.click();
  await expect(workflow).toBeChecked();
  const binding = page.getByRole("article", { name: "Binding builder" });
  await expect(binding).toBeVisible();
  await expect(page.getByText("Published agents unavailable")).toBeVisible();
  await expect(page.getByText("No published agents yet.")).toHaveCount(0);
  await expect(page.getByText(/Failed to fetch|private/i)).toHaveCount(0);
  const retry = page.getByRole("button", { name: "Retry published agents" });
  await expect(retry).toHaveCount(1);
  const newRunUrl = page.url();

  observed.length = 0;
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Published agents incomplete")).toBeVisible();
  await expect(retry).toBeFocused();
  await expect(page.getByText(/Failed to fetch|private/i)).toHaveCount(0);
  expectOnlyAgentRead([agentTarget(), agentTarget(firstHash)]);
  expect(page.url()).toBe(newRunUrl);

  // A second, partial page still confirms nothing (#440's joint-page
  // atomicity): no agent option is offered while the read is incomplete.
  await expect(binding.getByLabel("Agent for builder")).toHaveCount(0);

  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(retry).toBeFocused();
  await expectVisibleFocus(retry);
  await assertNoSeriousAccessibilityFindings(page);
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({
    path: "test-results/read-recovery-new-run-agent-grayscale-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/read-recovery-new-run-agent-grayscale-390x844.png",
    fullPage: true
  });
  await page.locator("style").last().evaluate((element) => element.remove());
  await page.setViewportSize({ width: 1280, height: 900 });

  observed.length = 0;
  await retry.click();
  await expect(page.getByText("Published agents incomplete")).toHaveCount(0);
  const picker = binding.getByLabel("Agent for builder");
  await expect(picker).toContainText("anthropic · sonnet · Subscription");
  await expect(picker).toContainText("openai · codex · Subscription");
  await expect(picker).toContainText("google · gemini · Subscription");
  expectOnlyAgentRead([agentTarget(), agentTarget(firstHash)]);
  expect(page.url()).toBe(newRunUrl);

  await picker.selectOption(chosenHash);
  await binding.locator("summary").click();
  const expertValues = {
    "Profile ID": "manual-profile",
    Revision: "7",
    Provider: "manual-provider",
    Model: "manual-model",
    Executor: "manual/v1"
  } as const;
  for (const [label, value] of Object.entries(expertValues)) {
    await binding.getByLabel(label).fill(value);
  }
  await binding.getByLabel("Auth mode").selectOption("api_key");
  await expect(picker).toHaveValue(chosenHash);
  for (const [label, value] of Object.entries(expertValues)) {
    await expect(binding.getByLabel(label)).toHaveValue(value);
  }
  await expect(binding.getByLabel("Auth mode")).toHaveValue("api_key");
  // One freshness model, once confirmed: no manual refresh remains. #440's
  // "preserves...through refresh failure" clause has no reachable trigger
  // left on this surface once a read is confirmed (#532 removes the last
  // manual refresh); the state machine still preserves confirmed truth and
  // draft state (readResource.test.ts), but this UI no longer offers a way
  // to force a second, later failure against an already-confirmed list.
  await expect(page.getByRole("button", { name: /published agents/ })).toHaveCount(0);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({
    path: "test-results/read-recovery-new-run-agent-desktop.png",
    fullPage: true
  });
});

test("proves(new-run-confirms-workflow-detail-before-committing-selection-and-draft): New Run retains one exact immutable workflow detail", async ({ page }) => {
  const workflowListPath = "/atelier/api/v1/workflow-revisions";
  const name = "detail-recovery-proof";
  const confirmedHash = "1".repeat(64);
  const attemptedHash = "2".repeat(64);
  const detailTarget = (hash: string): string => `${workflowListPath}/${hash}`;
  const summary = (hash: string, description: string) => ({
    workflow_revision_hash: hash,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name,
    description
  });
  const detail = (hash: string, description: string) => ({
    workflow_revision_hash: hash,
    document_base64: "Zm9ybWF0X3ZlcnNpb246IDMK",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: [],
      orders: [],
      node_previews: [{
        id: "wait",
        kind: "wait",
        role: null,
        instruction_start: null,
        depends_on: []
      }],
      loops: [],
      name,
      description
    }
  });
  let secondJourney = false;
  let confirmedCalls = 0;
  let attemptedCalls = 0;
  let releaseLateConfirmed = (): void => {};
  const lateConfirmedGate = new Promise<void>((resolve) => { releaseLateConfirmed = resolve; });
  const observed: Array<{ method: string; target: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/atelier/api/v1")) {
      observed.push({ method: request.method(), target: `${url.pathname}${url.search}` });
    }
  });
  await page.route("**/atelier/api/v1/agent-configuration-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], next_after_revision_hash: null })
    });
  });
  await page.route("**/atelier/api/v1/workflow-revisions?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          summary(confirmedHash, "The retained confirmed revision."),
          summary(attemptedHash, "The attempted revision.")
        ],
        next_after_revision_hash: null
      })
    });
  });
  await page.route("**/atelier/api/v1/workflow-revisions/by-name/*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        display_name: name,
        lineage_id: "3".repeat(64),
        workflow_revision_hash: confirmedHash,
        revision_number: 2
      })
    });
  });
  await page.route(/\/atelier\/api\/v1\/workflow-revisions\/[0-9a-f]{64}$/, async (route) => {
    const hash = new URL(route.request().url()).pathname.split("/").at(-1);
    if (hash === confirmedHash) {
      confirmedCalls += 1;
      if (secondJourney) {
        await lateConfirmedGate;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(detail(confirmedHash, "Late confirmed detail."))
        });
        return;
      }
      if (confirmedCalls <= 2) {
        await route.abort("failed");
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(detail(confirmedHash, "The retained confirmed revision."))
      });
      return;
    }
    attemptedCalls += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        !secondJourney && attemptedCalls === 1
          ? detail(confirmedHash, "Mismatched detail.")
          : detail(attemptedHash, "The attempted revision.")
      )
    });
  });
  const expectOnlyDetailGets = (targets: string[]): void => {
    expect(observed.every(({ method }) => method === "GET")).toBe(true);
    expect(observed.map(({ target }) => target).sort()).toEqual([...targets].sort());
  };

  await page.goto("/atelier/new");
  const row = page.getByRole("article", { name });
  await expect(row).toBeVisible();
  const newRunUrl = page.url();
  observed.length = 0;

  await row.getByText("Details", { exact: true }).click();
  await expect(page.getByText("Workflow detail unavailable")).toBeVisible();
  await expect(page.getByText(/Failed to fetch|private/i)).toHaveCount(0);
  let retry = page.getByRole("button", { name: "Retry workflow detail" });
  await expect(retry).toHaveCount(1);
  expectOnlyDetailGets([detailTarget(confirmedHash)]);

  observed.length = 0;
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Workflow detail unavailable")).toBeVisible();
  await expect(retry).toBeFocused();
  expectOnlyDetailGets([detailTarget(confirmedHash)]);

  observed.length = 0;
  await retry.click();
  await expect(row).toContainText("1 nodes");
  await expect(retry).toHaveCount(0);
  expectOnlyDetailGets([detailTarget(confirmedHash)]);
  observed.length = 0;
  await row.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByLabel("Exact workflow YAML")).toHaveValue("format_version: 3\n");
  expectOnlyDetailGets([]);
  await expect(page.getByRole("button", { name: "Refresh workflow detail" })).toHaveCount(0);

  await row.getByRole("radio").check();
  const runId = await page.getByRole("heading", { name: "Run ID" }).locator("..").locator("code").textContent();
  const revisionChoice = row.getByLabel(`Revision of ${name}`);
  observed.length = 0;
  await revisionChoice.selectOption(attemptedHash);
  await expect(page.getByText("Workflow detail unavailable")).toBeVisible();
  await expect(revisionChoice).toHaveValue(confirmedHash);
  await expect(page.getByRole("heading", { name: "Run ID" }).locator("..").locator("code")).toHaveText(runId ?? "");
  expectOnlyDetailGets([detailTarget(attemptedHash)]);
  expect(page.url()).toBe(newRunUrl);

  retry = page.getByRole("button", { name: "Retry workflow detail" });
  await retry.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(retry).toBeFocused();
  await expectVisibleFocus(retry);
  await assertNoSeriousAccessibilityFindings(page);
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({
    path: "test-results/read-recovery-new-run-detail-grayscale-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/read-recovery-new-run-detail-grayscale-390x844.png",
    fullPage: true
  });
  await page.locator("style").last().evaluate((element) => element.remove());
  await page.setViewportSize({ width: 1280, height: 900 });

  observed.length = 0;
  await retry.click();
  await expect(page.getByText("Workflow detail unavailable")).toHaveCount(0);
  await expect(revisionChoice).toHaveValue(attemptedHash);
  await expect(page.getByRole("heading", { name: "Run ID" }).locator("..").locator("code")).not.toHaveText(runId ?? "");
  await expect(page.getByRole("button", { name: "Refresh workflow detail" })).toHaveCount(0);
  expectOnlyDetailGets([detailTarget(attemptedHash)]);
  expect(page.url()).toBe(newRunUrl);
  await page.screenshot({
    path: "test-results/read-recovery-new-run-detail-desktop.png",
    fullPage: true
  });

  secondJourney = true;
  await page.reload();
  const reloadedRow = page.getByRole("article", { name });
  await expect(reloadedRow).toBeVisible();
  observed.length = 0;
  await reloadedRow.getByText("Details", { exact: true }).click();
  await expect(page.getByText("Looking…")).toBeVisible();
  await expect(page.getByRole("button", { name: /workflow detail/ })).toHaveCount(0);
  const reloadedChoice = reloadedRow.getByLabel(`Revision of ${name}`);
  await reloadedChoice.selectOption(attemptedHash);
  await expect(reloadedChoice).toHaveValue(attemptedHash);
  await reloadedRow.getByRole("radio").check();
  await expect(page.getByRole("heading", { name: "Run ID" })).toBeVisible();
  await page.getByLabel("Publish YAML").check();
  releaseLateConfirmed();
  await expect(page.getByRole("button", { name: "Start" })).toHaveCount(0);
  await page.getByLabel("Saved workflow").check();
  await reloadedRow.getByText("Details", { exact: true }).click();
  await expect(reloadedRow.getByLabel(`Revision of ${name}`)).toHaveValue(attemptedHash);
  expectOnlyDetailGets([detailTarget(confirmedHash), detailTarget(attemptedHash)]);
  expect(page.url()).toBe(newRunUrl);
});

test("walks the whole workshop: board into the run, and the trail back up through the project", async ({ page }) => {
  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();

  await page
    .getByRole("region", { name: "Needs you" })
    .getByRole("link")
    .first()
    .click();
  // This V1 fixture declares no workflow name; the title says that honestly
  // instead of leading with the raw run id (#506).
  await expect(page.getByRole("heading", { name: "Unnamed workflow" })).toBeVisible();
  const trail = page.getByRole("navigation", { name: "Where you are" });
  await expect(trail.getByRole("link", { name: "Board" })).toBeVisible();
  await expect(trail.getByRole("link", { name: THE_ONE_PROJECT })).toBeVisible();
  await page.screenshot({ path: "test-results/run-trail-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);

  await trail.getByRole("link", { name: THE_ONE_PROJECT }).click();
  await expect(page.getByRole("heading", { name: THE_ONE_PROJECT })).toBeVisible();
  await page
    .getByRole("navigation", { name: "Where you are" })
    .getByRole("link", { name: "Board" })
    .click();
  await expect(page.getByRole("heading", { name: "Board" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier$/);
});

test("mobile Found and Absent reconcile exact durable runs", async ({ browser }) => {
  const mobile = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: "no-preference"
  });
  const page = await mobile.newPage();

  await page.goto(`/atelier/runs/${foundReference}`);
  await expect(page.getByRole("heading", { name: "Decision needed" })).toBeVisible();
  await expect(page.getByText("WAITING RECONCILIATION", { exact: false })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/reconciliation-needs-390x844.png",
    fullPage: true
  });
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({
    path: "test-results/reconciliation-needs-grayscale-390x844.png",
    fullPage: true
  });
  await page.locator("style").last().evaluate((element) => element.remove());

  const foundActor = page.getByLabel("Actor");
  const foundEvidence = page.getByLabel("Evidence", { exact: true });
  const foundChoice = page.getByRole("radio", { name: "Found" });
  const foundEffect = page.getByLabel("Effect ID");
  const foundResult = page.getByLabel("Exact result (base64)");
  const resolve = page.getByRole("button", { name: "Resolve" });
  await foundActor.focus();
  await foundActor.fill("Felix");
  await page.keyboard.press("Tab");
  await expect(foundEvidence).toBeFocused();
  await foundEvidence.fill("Inspected exact destination");
  await page.keyboard.press("Tab");
  await expect(foundChoice).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(foundEffect).toBeFocused();
  await foundEffect.fill("found-empty-effect");
  await page.keyboard.press("Tab");
  await expect(foundResult).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(resolve).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: /Decision/ })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Answer needed" })).toBeVisible();

  await page.goto(`/atelier/runs/${absentReference}`);
  await expect(page.getByRole("heading", { name: "Decision needed" })).toBeVisible();
  const absentActor = page.getByLabel("Actor");
  const absentEvidence = page.getByLabel("Evidence", { exact: true });
  const absentChoice = page.getByRole("radio", { name: "Absent" });
  await absentActor.focus();
  await absentActor.fill("Felix");
  await page.keyboard.press("Tab");
  await expect(absentEvidence).toBeFocused();
  await absentEvidence.fill("No matching destination effect");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("radio", { name: "Found" })).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(absentChoice).toBeChecked();
  await expect(absentChoice).toBeFocused();
  const review = page.getByRole("button", { name: "Review" });
  await page.keyboard.press("Tab");
  await expect(review).toBeFocused();
  await page.keyboard.press("Enter");
  const cancel = page.getByRole("button", { name: "Cancel" });
  const execute = page.getByRole("button", { name: "Execute" });
  await expect(page.getByRole("dialog", { name: "Execute this exact effect?" })).toBeVisible();
  await expect(cancel).toBeFocused();
  await expect(page.getByText("Atelier will execute the exact request once.")).toBeVisible();
  await page.screenshot({ path: "test-results/absent-confirm-390x844.png", fullPage: true });
  await page.keyboard.press("Shift+Tab");
  await expect(execute).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(cancel).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(execute).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(review).toBeFocused();
  await review.press("Enter");
  await expect(cancel).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(review).toBeFocused();
  await review.press("Enter");
  await expect(cancel).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(execute).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: /Sending decision|Decision pending/ })).toBeFocused();
  await expect(page.getByRole("heading", { name: /Decision/ })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Answer needed" })).toBeVisible();
  await page.getByLabel("Integer answer").fill("5");
  await page.getByRole("button", { name: "Answer" }).click();
  await expect(page.getByText("completed", { exact: true })).toBeVisible();
  await assertNoSeriousAccessibilityFindings(page);
  await mobile.close();

  const desktop = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    reducedMotion: "reduce"
  });
  const desktopPage = await desktop.newPage();
  await desktopPage.goto(`/atelier/runs/${absentReference}`);
  await expect(desktopPage.getByText("completed", { exact: true })).toBeVisible();
  await expect(desktopPage.locator(".connection-complete")).toContainText("Complete");
  await assertNoSeriousAccessibilityFindings(desktopPage);
  await desktopPage.screenshot({
    path: "test-results/complete-desktop-reduced-motion.png",
    fullPage: true
  });
  await desktop.close();
});

async function assertMobileSurface(page: Page): Promise<void> {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
  const surfaces = page.locator("[role=alert], article");
  for (let index = 0; index < await surfaces.count(); index += 1) {
    const clipped = await surfaces.nth(index).evaluate(
      (element) => element.scrollWidth - element.clientWidth
    );
    expect(clipped, `surface ${index} must not clip content`).toBeLessThanOrEqual(0);
  }
  const controls = page.locator(
    "button, input[type=text], textarea, select, .determination-picker label, summary"
  );
  for (let index = 0; index < await controls.count(); index += 1) {
    const box = await controls.nth(index).boundingBox();
    expect(box, `control ${index} must be rendered`).not.toBeNull();
    expect(box?.height, `control ${index} must have a 44px touch target`).toBeGreaterThanOrEqual(44);
  }
  await assertNoSeriousAccessibilityFindings(page);
}

async function assertNoSeriousAccessibilityFindings(page: Page): Promise<void> {
  const scan = await new AxeBuilder({ page }).analyze();
  expect(
    scan.violations.filter((violation) =>
      violation.impact === "serious" || violation.impact === "critical"
    )
  ).toEqual([]);
}

async function expectVisibleFocus(control: Locator): Promise<void> {
  const outline = await control.evaluate((element) => {
    const style = getComputedStyle(element);
    return { style: style.outlineStyle, width: Number.parseFloat(style.outlineWidth) };
  });
  expect(outline.style).not.toBe("none");
  expect(outline.width).toBeGreaterThanOrEqual(3);
}

test("opens a V3 run at its own address and shows the line it drove", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    "name: Two agents in a line",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    ...declaredOutput(schemaHash),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Check what the node before you did.",
    "    depends_on: [implement]",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: { profile_id: "v3-local", revision_number: 1, provider_id: "e2e-v3", auth_mode: "subscription" }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/seen-in-the-browser",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const createdRun = await started.json();
  expect(createdRun.workflow_format_version).toBe(3);
  const reference = createdRun.public_run_reference as string;

  // The runtime drives the line without any further request; the read route is
  // what says it has, which is the vertical this page then renders.
  let terminal: string | null = null;
  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("COMPLETED");
    expect(body.node_rail.map((entry: { node_id: string }) => entry.node_id)).toEqual([
      "implement",
      "review"
    ]);
    terminal = body.terminal_hash as string;
  }).toPass({ timeout: 15_000 });
  expect(terminal).not.toBeNull();
  if (terminal === null) {
    throw new Error("expected the completed run to name a terminal hash");
  }

  await page.goto(`/atelier/runs/${reference}`);

  await expect(page.getByRole("heading", { level: 1, name: "Two agents in a line" })).toBeVisible();
  const identity = page.getByRole("button", { name: "Run id" });
  await expect(page.getByText("v3/seen-in-the-browser")).toHaveCount(0);
  await identity.click();
  await expect(page.getByText("v3/seen-in-the-browser")).toBeVisible();
  const graph = page.getByRole("region", { name: "Workflow" });
  await expect(graph.getByRole("button", { name: "implement — Done" })).toBeVisible();
  await expect(graph.getByRole("button", { name: "review — Done" })).toBeVisible();
  await expect(page.getByLabel("Where this run stands")).toContainText("Done");
  await expect(page.getByLabel("Where this run stands")).not.toContainText("Snapshot");
  const terminalProof = page.getByRole("button", { name: "Terminal hash" });
  await expect(terminalProof).toBeVisible();
  await expect(page.getByText(terminal)).toHaveCount(0);
  await terminalProof.click();
  await expect(page.getByText(terminal)).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);

  await page.screenshot({ path: "test-results/v3-run-desktop.png", fullPage: true });
  await page.screenshot({ path: "test-results/v3-graph-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { level: 1, name: "Two agents in a line" })).toBeVisible();
  expect(await page.evaluate(() => globalThis.document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/v3-run-mobile.png", fullPage: true });
  await page.screenshot({ path: "test-results/v3-graph-390x844.png", fullPage: true });
});

test("starts a published V3 workflow by picking a named agent", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: "named-picker",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "named-sonnet",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);

  await page.goto("/atelier/new");
  await page.getByLabel("Publish YAML").check();
  await page.getByLabel("Exact workflow YAML").fill(
    [
      "format_version: 3",
      "name: Started with a named agent",
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Do the one thing this chain is for.",
      ...declaredOutput(schemaHash),
      "  - id: review",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Check what the node before you did.",
      "    depends_on: [implement]",
      ...declaredOutput(schemaHash),
      ""
    ].join("\n")
  );
  await page.getByRole("button", { name: "Review publication" }).click();
  await page.getByRole("button", { name: "Publish", exact: true }).click();

  const binding = page.getByRole("article", { name: "Binding builder" });
  await expect(binding).toBeVisible();
  const picker = binding.getByLabel("Agent for builder");
  await expect(picker).toContainText("e2e-v3 · named-sonnet · Subscription");
  await picker.selectOption({ label: "e2e-v3 · named-sonnet · Subscription" });
  await page.screenshot({ path: "test-results/named-agent-picker-desktop.png", fullPage: true });

  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Started with a named agent" })).toBeVisible();
  await expect(page.getByLabel("Where this run stands")).toContainText("Done", {
    timeout: 20_000
  });
  await page.screenshot({ path: "test-results/named-agent-run-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { level: 1, name: "Started with a named agent" })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/named-agent-run-390x844.png", fullPage: true });
});

test("proves(a-listed-agent-configuration-names-current-startability): keeps a remembered listed-unavailable agent visible until keyboard recovery", async ({ page }) => {
  const api = "/atelier/api/v1";
  const workflowName = "unavailable-named-agent";
  const schemaHash = await anyJsonSchema(page);
  const workflow = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Choose a working executor.",
      ...declaredOutput(schemaHash),
      ""
    ].join("\n")
  });
  expect(workflow.status()).toBe(201);
  const workflowHash = (await workflow.json()).workflow_revision_hash as string;
  const founded = await page.request.post(`${api}/workflow-lineages`, {
    data: {
      workflow_revision_hash: workflowHash,
      actor: "e2e",
      activated_at: "2026-08-22T00:00:00Z"
    }
  });
  expect(founded.status()).toBe(201);
  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: "unavailable-picker",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const authHash = (await auth.json()).auth_profile_revision_hash as string;
  const publishConfiguration = async (model: string, capability: "headless" | "headless_with_tools") => {
    const response = await page.request.post(`${api}/agent-configuration-revisions`, {
      data: {
        model,
        auth_profile_revision_hash: authHash,
        executor_revision: "immediate/v1",
        requested_capability: capability
      }
    });
    expect(response.status()).toBe(201);
    return await response.json() as Record<string, unknown>;
  };
  const unavailable = await publishConfiguration("remembered-unavailable", "headless_with_tools");
  const healthy = await publishConfiguration("keyboard-healthy", "headless");
  const unavailableHash = unavailable.agent_configuration_revision_hash as string;
  const healthyHash = healthy.agent_configuration_revision_hash as string;
  const listedItems: Array<{
    agent_configuration_revision_hash: string;
    startable: boolean;
    not_startable_reason: string | null;
  }> = [];
  let after: string | null = null;
  do {
    const listed = await page.request.get(
      after === null
        ? `${api}/agent-configuration-revisions?limit=100`
        : `${api}/agent-configuration-revisions?limit=100&after_revision_hash=${after}`
    );
    expect(listed.status()).toBe(200);
    const pageBody = await listed.json() as {
      items: typeof listedItems;
      next_after_revision_hash: string | null;
    };
    listedItems.push(...pageBody.items);
    after = pageBody.next_after_revision_hash;
  } while (after !== null);
  expect(listedItems.find((item) => item.agent_configuration_revision_hash === unavailableHash)).toEqual(
    expect.objectContaining({
      startable: false,
      not_startable_reason: "agent-executor-binding-unavailable"
    })
  );
  expect(listedItems.find((item) => item.agent_configuration_revision_hash === healthyHash)).toEqual(
    expect.objectContaining({
      startable: true,
      not_startable_reason: null
    })
  );
  await page.addInitScript(
    ([key, hash]) => localStorage.setItem(key, JSON.stringify({ builder: hash })),
    [NAMED_AGENT_CHOICE_STORAGE_KEY, unavailableHash] as const
  );
  const starts: Array<Record<string, unknown>> = [];
  const occupancyWrites: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname === `${api}/runs`) {
      starts.push(request.postDataJSON() as Record<string, unknown>);
    }
    if (request.method() === "PUT" && url.pathname.includes("/occupancy/")) {
      occupancyWrites.push(url.pathname);
    }
  });

  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: new RegExp(workflowName) }).click();
  const binding = page.getByRole("article", { name: "Binding builder" });
  const picker = binding.getByLabel("Agent for builder");
  await expect(picker).toHaveValue(unavailableHash);
  await expect(binding.getByLabel("Binding source: Remembered")).toBeVisible();
  await expect(binding.getByRole("status")).toContainText("◇ Unavailable");
  const unavailableHint = binding.getByRole("button", { name: "Why builder is unavailable" });
  await unavailableHint.focus();
  await unavailableHint.press("Enter");
  await expect(unavailableHint).toHaveAttribute("aria-expanded", "true");
  await expect(binding.locator(".info-popover code")).toHaveText(
    "This deployment cannot start this executor. Choose another agent or repair its startup check."
  );
  expect(
    await picker
      .getByRole("option", { name: /remembered-unavailable.*Unavailable/ })
      .evaluate((option) => (option as HTMLOptionElement).disabled)
  ).toBe(true);
  await expect(page.getByRole("button", { name: "Start" })).toHaveCount(0);
  expect(starts).toEqual([]);
  await picker.focus();
  await expect(picker).toBeFocused();
  await expectVisibleFocus(picker);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({ path: "test-results/unavailable-agent-picker-desktop.png", fullPage: true });
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({ path: "test-results/unavailable-agent-picker-grayscale-desktop.png", fullPage: true });
  await page.locator("style").last().evaluate((element) => element.remove());

  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/unavailable-agent-picker-unavailable-390x844.png",
    fullPage: true
  });

  await expect(picker.getByRole("option", { name: /keyboard-healthy/ })).toHaveCount(1);
  await picker.focus();
  await page.keyboard.press("Home");
  const enabledCount = await picker.locator("option:not([disabled])").count();
  for (let step = 0; step < enabledCount; step += 1) {
    if ((await picker.inputValue()) === healthyHash) break;
    await page.keyboard.press("ArrowDown");
  }
  await expect(picker).toHaveValue(healthyHash);
  await expect(binding.getByLabel("Binding source: Remembered")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start" })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/unavailable-agent-picker-recovered-390x844.png",
    fullPage: true
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.screenshot({ path: "test-results/unavailable-agent-picker-reduced-motion.png", fullPage: true });

  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByRole("heading", { level: 1, name: workflowName })).toBeVisible({
    timeout: 20_000
  });
  await expect(page.getByLabel("Where this run stands")).toContainText("Done", {
    timeout: 20_000
  });
  expect(starts).toHaveLength(1);
  expect((starts[0]?.agent_bindings as Array<{ agent_configuration_revision_hash: string }>)).toEqual([
    { role: "builder", agent_configuration_revision_hash: healthyHash }
  ]);
  expect(occupancyWrites).toEqual([]);
});

test("proves(new-run-prefers-project-occupancy-before-local-memory-and-empty): starts the visible project bindings", async ({ page }) => {
  const api = "/atelier/api/v1";
  const workflowName = "project-occupancy-browser";
  const schemaHash = await anyJsonSchema(page);
  const publishAgent = async (
    profileId: string,
    providerId: string,
    model: string
  ): Promise<string> => {
    const auth = await page.request.post(`${api}/auth-profile-revisions`, {
      data: {
        profile_id: profileId,
        revision_number: 1,
        provider_id: providerId,
        auth_mode: "subscription"
      }
    });
    expect(auth.status()).toBe(201);
    const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
      data: {
        model,
        auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
        executor_revision: "immediate/v1",
        requested_capability: "headless"
      }
    });
    expect(configuration.status()).toBe(201);
    return (await configuration.json()).agent_configuration_revision_hash as string;
  };
  const projectHash = await publishAgent(
    "occupancy-project",
    "e2e-v3",
    "project-choice"
  );
  const rememberedHash = await publishAgent(
    "occupancy-remembered",
    "e2e-v3",
    "remembered-choice"
  );
  const workflow = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      `name: ${workflowName}`,
      "description: The project chooses before this browser does.",
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Build the requested slice.",
      ...declaredOutput(schemaHash, "built"),
      "  - id: review",
      "    type: agent",
      "    role: reviewer",
      "    mode: headless",
      "    instruction: Review the built slice.",
      "    depends_on: [implement]",
      ...declaredOutput(schemaHash, "reviewed"),
      "  - id: audit",
      "    type: agent",
      "    role: auditor",
      "    mode: headless",
      "    instruction: Audit the reviewed slice.",
      "    depends_on: [review]",
      ...declaredOutput(schemaHash, "audited"),
      ""
    ].join("\n")
  });
  expect(workflow.status()).toBe(201);
  const workflowHash = (await workflow.json()).workflow_revision_hash as string;
  const founded = await page.request.post(`${api}/workflow-lineages`, {
    data: {
      workflow_revision_hash: workflowHash,
      actor: "e2e",
      activated_at: "2026-08-21T00:00:00Z"
    }
  });
  expect(founded.status()).toBe(201);
  const lineageId = (await founded.json()).lineage_id as string;
  const projects = await page.request.get(`${api}/projects`);
  expect(projects.status()).toBe(200);
  const projectReference = (await projects.json()).items[0]
    .public_project_reference as string;
  const occupancyPath = `${api}/projects/${projectReference}/occupancy/${lineageId}`;
  const written = await page.request.put(occupancyPath, {
    data: {
      revision_number: 1,
      bindings: [
        { role: "builder", agent_configuration_revision_hash: projectHash },
        { role: "foreign", agent_configuration_revision_hash: rememberedHash }
      ]
    }
  });
  expect(written.status()).toBe(201);

  await page.addInitScript(
    ([storageKey, hash]) => {
      localStorage.setItem(storageKey, JSON.stringify({
        builder: hash,
        reviewer: hash
      }));
    },
    [NAMED_AGENT_CHOICE_STORAGE_KEY, rememberedHash] as const
  );
  const observed: Array<{ method: string; path: string }> = [];
  const started: {
    bindings: Array<{
      role: string;
      agent_configuration_revision_hash: string;
    }> | null;
  } = { bindings: null };
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith(api)) {
      observed.push({ method: request.method(), path: url.pathname });
    }
    if (request.method() === "POST" && url.pathname === `${api}/runs`) {
      started.bindings = (request.postDataJSON() as {
        agent_bindings: Array<{
          role: string;
          agent_configuration_revision_hash: string;
        }>;
      }).agent_bindings;
    }
  });

  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: new RegExp(workflowName) }).click();
  const builder = page.getByRole("article", { name: "Binding builder" });
  const reviewer = page.getByRole("article", { name: "Binding reviewer" });
  const auditor = page.getByRole("article", { name: "Binding auditor" });
  await expect(builder.getByLabel("Binding source: Project")).toBeVisible();
  await expect(reviewer.getByLabel("Binding source: Remembered")).toBeVisible();
  await expect(auditor.getByLabel("Binding source: Choose")).toBeVisible();
  await expect(builder.getByLabel("Agent for builder")).toHaveValue(projectHash);
  await expect(reviewer.getByLabel("Agent for reviewer")).toHaveValue(rememberedHash);
  await expect(auditor.getByLabel("Agent for auditor")).toHaveValue("");
  await expect(page.getByRole("article", { name: "Binding foreign" })).toHaveCount(0);
  expect(
    observed.filter(({ path }) =>
      path === `${api}/projects` || path.includes("/occupancy/")
    )
  ).toEqual([
    { method: "GET", path: `${api}/projects` },
    { method: "GET", path: occupancyPath }
  ]);
  expect(
    observed.filter(({ path }) =>
      path === `${api}/workflow-revisions/by-name/${workflowName}`
    )
  ).toHaveLength(1);

  const auditorPicker = auditor.getByLabel("Agent for auditor");
  await auditorPicker.focus();
  await expect(auditorPicker).toBeFocused();
  await expectVisibleFocus(auditorPicker);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({
    path: "test-results/project-occupancy-picker-desktop.png",
    fullPage: true
  });
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({
    path: "test-results/project-occupancy-picker-grayscale-desktop.png",
    fullPage: true
  });
  await page.locator("style").last().evaluate((element) => element.remove());
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({
    path: "test-results/project-occupancy-picker-390x844.png",
    fullPage: true
  });

  await auditorPicker.selectOption(projectHash);
  await expect(auditor.getByLabel("Binding source: Remembered")).toBeVisible();
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByRole("heading", { level: 1, name: workflowName })).toBeVisible({
    timeout: 20_000
  });
  expect(Object.fromEntries(
    (started.bindings ?? []).map((binding) => [
      binding.role,
      binding.agent_configuration_revision_hash
    ])
  )).toEqual({
    auditor: projectHash,
    builder: projectHash,
    reviewer: rememberedHash
  });
});

test("proves(project-occupancy-editor-confirms-complete-project-truth): edits one project recommendation through the cockpit", async ({ page }) => {
  const workflowName = "occupancy-editor-browser";
  const schemaHash = await anyJsonSchema(page);
  await page.goto("/atelier/new");
  await page.getByLabel("Publish YAML").check();
  await page.getByLabel("Exact workflow YAML").fill([
    "format_version: 3",
    `name: ${workflowName}`,
    "nodes:",
    "  - id: builder",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Build the one thing.",
    ...declaredOutput(schemaHash, "built"),
    "  - id: reviewer",
    "    type: agent",
    "    role: reviewer",
    "    mode: headless",
    "    instruction: Review the thing.",
    "    depends_on: [builder]",
    ...declaredOutput(schemaHash, "reviewed"),
    "  - id: auditor",
    "    type: agent",
    "    role: auditor",
    "    mode: headless",
    "    instruction: Audit the thing.",
    "    depends_on: [reviewer]",
    ...declaredOutput(schemaHash, "audited"),
    ""
  ].join("\n"));
  await page.getByRole("button", { name: "Review publication" }).click();
  await page.getByRole("button", { name: "Publish", exact: true }).click();

  for (const [role, model] of [["builder", "editor-builder"], ["reviewer", "editor-reviewer"], ["auditor", "editor-auditor"]] as const) {
    const binding = page.getByRole("article", { name: `Binding ${role}` });
    await expect(binding).toBeVisible();
    await binding.getByText("Expert fields", { exact: true }).click();
    await binding.getByLabel("Profile ID").fill(`occupancy-${role}`);
    await binding.getByLabel("Revision").fill("1");
    await binding.getByLabel("Provider").fill("e2e-v3");
    await binding.getByLabel("Auth mode").selectOption("subscription");
    await binding.getByLabel("Model").fill(model);
    await binding.getByLabel("Executor").fill("immediate/v1");
  }
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByRole("heading", { level: 1, name: workflowName })).toBeVisible({ timeout: 20_000 });

  await page.goto("/atelier/project");
  const workflow = page.getByRole("combobox", { name: "Workflow occupancy" });
  await expect(workflow).toBeVisible();
  await workflow.selectOption({ label: workflowName });
  const builder = page.getByRole("combobox", { name: "Recommendation for builder" });
  const reviewer = page.getByRole("combobox", { name: "Recommendation for reviewer" });
  const auditor = page.getByRole("combobox", { name: "Recommendation for auditor" });
  await expect(builder).toBeVisible();
  await expect(page.getByText("No project recommendations yet.")).toBeVisible();
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({ path: "test-results/project-occupancy-editor-empty.png", fullPage: true });
  await builder.selectOption({ label: "e2e-v3 · editor-builder · Subscription" });
  await reviewer.selectOption({ label: "e2e-v3 · editor-reviewer · Subscription" });
  await auditor.selectOption({ label: "e2e-v3 · editor-auditor · Subscription" });
  const builderHash = await builder.inputValue();
  const reviewerHash = await reviewer.inputValue();
  const auditorHash = await auditor.inputValue();
  const save = page.getByRole("button", { name: "Save" });
  await expect(save).toBeEnabled();
  await workflow.focus();
  await expect(workflow).toBeFocused();
  await expectVisibleFocus(workflow);
  await builder.focus();
  await expect(builder).toBeFocused();
  await expectVisibleFocus(builder);
  await save.focus();
  await expect(save).toBeFocused();
  await expectVisibleFocus(save);
  await save.click();
  await expect(save).toBeDisabled();
  await expect(page.locator(".occupancy-confirmed")).toBeVisible();
  await expect(page.getByText("Saving occupancy…")).toHaveCount(0);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({ path: "test-results/project-occupancy-editor-desktop.png", fullPage: true });
  await page.addStyleTag({ content: "html { filter: grayscale(1); }" });
  await page.screenshot({ path: "test-results/project-occupancy-editor-grayscale-desktop.png", fullPage: true });
  await page.locator("style").last().evaluate((element) => element.remove());
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({ path: "test-results/project-occupancy-editor-390x844.png", fullPage: true });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: new RegExp(workflowName) }).click();
  await page.getByRole("article", { name: "Binding builder" }).getByLabel("Agent for builder").selectOption(builderHash);
  await page.getByRole("article", { name: "Binding reviewer" }).getByLabel("Agent for reviewer").selectOption(auditorHash);
  await expect(page.getByRole("article", { name: "Binding builder" }).getByLabel("Binding source: Remembered")).toBeVisible();
  await expect(page.getByRole("article", { name: "Binding reviewer" }).getByLabel("Binding source: Remembered")).toBeVisible();

  await page.goto("/atelier/project");
  await page.getByRole("combobox", { name: "Workflow occupancy" }).selectOption({ label: workflowName });
  await expect(page.getByRole("combobox", { name: "Recommendation for reviewer" })).toBeVisible();
  await page.getByRole("combobox", { name: "Recommendation for builder" }).selectOption(reviewerHash);
  await page.getByRole("combobox", { name: "Recommendation for reviewer" }).selectOption("");
  await page.getByRole("combobox", { name: "Recommendation for auditor" }).selectOption("");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();
  await expect(page.locator(".occupancy-confirmed")).toBeVisible();

  const restarted = await page.request.post("/__e2e/recompose");
  expect(restarted.status()).toBe(202);
  const expectedGeneration = await restarted.text();
  await expect(async () => {
    expect(await (await page.request.get("/__e2e/generation")).text()).toBe(expectedGeneration);
  }).toPass({ timeout: 20_000 });
  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: new RegExp(workflowName) }).click();
  await expect(page.getByRole("article", { name: "Binding builder" }).getByLabel("Binding source: Project")).toBeVisible();
  await expect(page.getByRole("article", { name: "Binding reviewer" }).getByLabel("Binding source: Remembered")).toBeVisible();
  await expect(page.getByRole("article", { name: "Binding auditor" }).getByLabel("Binding source: Choose")).toBeVisible();
  await expect(page.getByRole("article", { name: "Binding builder" }).getByLabel("Agent for builder")).toHaveValue(reviewerHash);
  await expect(page.getByRole("article", { name: "Binding reviewer" }).getByLabel("Agent for reviewer")).toHaveValue(auditorHash);

  const unavailable = async (route: Route) => route.abort();
  await page.route("**/atelier/api/v1/projects", unavailable);
  await page.goto("/atelier/project");
  await expect(page.getByText("Project occupancy unavailable")).toBeVisible();
  await page.screenshot({ path: "test-results/project-occupancy-editor-unavailable.png", fullPage: true });
  await page.unroute("**/atelier/api/v1/projects", unavailable);

  await page.reload();
  await page.getByRole("combobox", { name: "Workflow occupancy" }).selectOption({ label: workflowName });
  await expect(page.getByRole("combobox", { name: "Recommendation for builder" })).toBeVisible();
  await page.getByRole("combobox", { name: "Recommendation for builder" }).selectOption(builderHash);
  const conflict = async (route: Route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({
        status: 409,
        contentType: "application/problem+json",
        body: JSON.stringify({
          type: "urn:atelier2:problem:v1:occupancy-revision-conflict",
          title: "Occupancy revision conflict",
          status: 409,
          detail: "another operator changed the recommendation"
        })
      });
      return;
    }
    await route.continue();
  };
  await page.route("**/occupancy/**", conflict);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Occupancy changed elsewhere.")).toBeVisible();
  await page.screenshot({ path: "test-results/project-occupancy-editor-conflict.png", fullPage: true });
  await page.unroute("**/occupancy/**", conflict);
  await page.getByRole("button", { name: "Reload" }).click();
  await expect(page.getByRole("combobox", { name: "Recommendation for builder" })).toBeVisible();
  await page.getByRole("combobox", { name: "Recommendation for builder" }).selectOption(builderHash);
  let uncertainWriteBody: Buffer | null = null;
  const uncertain = async (route: Route) => {
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    uncertainWriteBody = route.request().postDataBuffer();
    await route.abort();
  };
  await page.route("**/occupancy/**", uncertain);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Occupancy save unconfirmed.")).toBeVisible();
  expect(uncertainWriteBody).not.toBeNull();
  await page.screenshot({ path: "test-results/project-occupancy-editor-uncertain.png", fullPage: true });
  await page.unroute("**/occupancy/**", uncertain);
  let retryWriteBody: Buffer | null = null;
  const observeRetry = async (route: Route) => {
    if (route.request().method() === "PUT") retryWriteBody = route.request().postDataBuffer();
    await route.continue();
  };
  await page.route("**/occupancy/**", observeRetry);
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.locator(".occupancy-confirmed")).toContainText("Saved");
  expect(retryWriteBody).not.toBeNull();
  expect(retryWriteBody).toEqual(uncertainWriteBody);
  await page.unroute("**/occupancy/**", observeRetry);
  const noWorkflows = async (route: Route) => {
    if (route.request().url().includes("/workflow-revisions?")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], next_after_revision_hash: null })
      });
      return;
    }
    await route.continue();
  };
  await page.route("**/workflow-revisions**", noWorkflows);
  await page.reload();
  await expect(page.getByText("No admitted workflows yet.")).toBeVisible();
  await page.screenshot({ path: "test-results/project-occupancy-editor-empty-catalog.png", fullPage: true });
  await page.unroute("**/workflow-revisions**", noWorkflows);
});

test("publishes a V3 workflow, binds its role, and watches the line it started", async ({ page }) => {
  const schemaHash = await anyJsonSchema(page);
  await page.goto("/atelier/new");
  await page.getByLabel("Publish YAML").check();
  await page.getByLabel("Exact workflow YAML").fill(
    [
      "format_version: 3",
      "name: Seen from the picker",
      "nodes:",
      "  - id: implement",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Do the one thing this chain is for.",
      ...declaredOutput(schemaHash),
      "  - id: review",
      "    type: agent",
      "    role: builder",
      "    mode: headless",
      "    instruction: Check what the node before you did.",
      "    depends_on: [implement]",
      ...declaredOutput(schemaHash),
      ""
    ].join("\n")
  );
  await page.getByRole("button", { name: "Review publication" }).click();
  await expect(page.getByRole("dialog", { name: "Publish this exact workflow?" })).toBeVisible();
  await page.getByRole("button", { name: "Publish", exact: true }).click();

  // The role comes from the API, not from the operator re-reading their own YAML.
  const binding = page.getByRole("article", { name: "Binding builder" });
  await expect(binding).toBeVisible();
  await binding.locator("summary").click();
  await page.getByLabel("Profile ID").fill("picker-v3");
  await page.getByLabel("Revision").fill("1");
  await page.getByLabel("Provider").fill("e2e-v3");
  await page.getByLabel("Auth mode").selectOption("subscription");
  await page.getByLabel("Model").fill("v3-model");
  await page.getByLabel("Executor").fill("immediate/v1");
  await page.screenshot({ path: "test-results/v3-picker-bindings-desktop.png", fullPage: true });

  await page.getByRole("button", { name: "Start" }).click();

  await expect(page.getByRole("heading", { level: 1, name: "Seen from the picker" })).toBeVisible();
  const graph = page.getByRole("region", { name: "Workflow" });
  await expect(graph.getByRole("button", { name: /implement/ })).toBeVisible();
  await expect(graph.getByRole("button", { name: /review/ })).toBeVisible();
  // The reload this used to need is gone with #270: the page follows the run it
  // just started, so the same truth arrives without the operator asking twice.
  await expect(page.getByLabel("Where this run stands")).toContainText("Done", {
    timeout: 20_000
  });
  await expect(graph.getByRole("button", { name: "review — Done" })).toBeVisible();
  await page.screenshot({ path: "test-results/v3-picker-run-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);
});


test("watches a V3 chain move, node by node, without a reload", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    "name: Two agents watched live",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    ...declaredOutput(schemaHash),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Check what the node before you did.",
    "    depends_on: [implement]",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: { profile_id: "v3-live", revision_number: 1, provider_id: "e2e-v3", auth_mode: "subscription" }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/watched-live",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  // Opened straight after the start, without waiting for the run to end: the
  // stream carries the line's events to the page as the runtime writes them,
  // and it carries the ones already written the same way -- which is what makes
  // this deterministic without making it a lie.
  await page.goto(`/atelier/runs/${reference}`);

  const arriving = page.getByRole("list", { name: "What finished" });
  await expect(arriving.getByRole("listitem")).toHaveCount(2, { timeout: 20_000 });
  await expect(arriving).toContainText("implement");
  await expect(arriving).toContainText("review");
  await expect(page.getByLabel("Where this run stands")).toContainText("Ended");

  await page.screenshot({ path: "test-results/v3-run-live.png", fullPage: true });
});

test("draws a running V3 chain as a graph while a node is still working", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    "name: Two agents drawn live",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Do the one thing this chain is for.",
    ...declaredOutput(schemaHash),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Check what the node before you did.",
    "    depends_on: [implement]",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: { profile_id: "v3-drawn", revision_number: 1, provider_id: "e2e-v3-slow", auth_mode: "subscription" }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "delayed/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/drawn-while-running",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await page.goto(`/atelier/runs/${reference}`);

  const graph = page.getByRole("region", { name: "Workflow" });
  await expect(graph).toBeVisible();
  await expect(graph.getByRole("button", { name: /implement/ })).toBeVisible();
  await expect(graph.getByRole("button", { name: /review/ })).toBeVisible();
  const working = graph.getByRole("button", { name: /Working$/ });
  await expect(working).toBeVisible({ timeout: 10_000 });
  await expect(working).toHaveAttribute("data-live", "true");
  await expect(page.getByRole("region", { name: "Now" })).toBeVisible();
  await expect(page.getByText("Process log stays in the lease.")).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  await expect(graph.locator('[data-node-id="implement"]')).toHaveAttribute("data-layer", "0");
  await expect(graph.locator('[data-node-id="review"]')).toHaveAttribute("data-layer", "1");

  await page.screenshot({ path: "test-results/v3-graph-running-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(graph.getByRole("button", { name: /Working$/ })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/v3-graph-running-390x844.png", fullPage: true });
});

test("a node whose answer its own contract refuses never reports success", async ({
  page
}) => {
  // The operator's own silence, reproduced in a browser -- and its cause moved.
  // `live/die-kette-sieht` stood on STARTED with nothing to read: its first node
  // answered prose while its author had pinned a schema, and the atelier wrote
  // `AGENT_COMPLETED` anyway. Since #57 that success is never written: the run
  // stops on the node that answered, and nothing on the page claims it is done.
  //
  // What the page still cannot say is why, and this test pins that gap rather
  // than hiding it: no durable record of the refusal exists yet, because nothing
  // writes `node-receipt/v3`. The panel's refusal wording keeps its own proof in
  // the cockpit component tests, which drive the read surface directly.
  const api = "/atelier/api/v1";

  const schemaHash = await publishSchema(page, '{"type": "object"}');

  const workflowYaml = [
    "format_version: 3",
    "name: the chain the operator watched",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write three German sentences about code review.",
    ...declaredOutput(schemaHash, "draft"),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Judge the draft you were handed.",
    "    depends_on: [implement]",
    "    inputs:",
    "      - name: draft",
    "        from:",
    "          node: implement",
    "          output: draft",
    ...declaredOutput(schemaHash, "findings"),
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: "v3-stuck",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/the-silent-one",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  // The provider answers a sentence where an object was declared, so the success
  // write refuses it: the run stops standing on the node that answered, and no
  // completion event is written for it.
  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("FAILED");
    expect(body.current_node_id).toBe("implement");
    expect(body.terminal_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(body.node_rail).toEqual([
      {
        node_id: "implement",
        state: "failed",
        attempt: { ordinal: 1, state: "FAILED" }
      },
      { node_id: "review", state: "queued", attempt: null }
    ]);
  }).toPass({ timeout: 15_000 });

  await page.goto(`/atelier/project`);
  await expect(page.getByRole("heading", { name: "Failed" })).toBeVisible();
  await expect(page.getByRole("link", { name: "v3/the-silent-one" })).toBeVisible();

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { level: 1, name: "the chain the operator watched" })).toBeVisible();
  await expect(page.getByText("v3/the-silent-one")).toHaveCount(0);
  await page.getByRole("button", { name: "Run id" }).click();
  await expect(page.getByText("v3/the-silent-one")).toBeVisible();
  await expect(page.getByLabel("Where this run stands")).toContainText("Failed");
  await expect(page.getByLabel("Where this run stands")).not.toContainText("Done");
  await expect(page.getByRole("button", { name: "implement — Failed" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Working/ })).toHaveCount(0);

  await page.getByRole("button", { name: "implement — Failed" }).click();
  // Nothing was written, so there is nothing to show as an output -- and the
  // panel says so honestly rather than dressing the silence as a value.
  await expect(page.getByRole("region", { name: "Prompt" })).toContainText(
    "Write three German sentences about code review."
  );
  await expect(page.getByRole("region", { name: "Output" })).toContainText("Nothing written.");
  await expect(page.getByRole("region", { name: "Output" })).not.toContainText("yet");
  await expect(page.getByText("a moment")).toHaveCount(0);
  await page.screenshot({ path: "test-results/v3-node-refusal.png", fullPage: true });
});

test("clicking a finished node shows its whole log", async ({ page }) => {
  // The other half of the panel: a node that did produce a value shows all of it.
  // The timeline keeps the value short so movement stays readable; the panel
  // shows the whole log the operator asked for.
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);

  const workflowYaml = [
    "format_version: 3",
    "name: the chain the operator read",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write three German sentences about code review.",
    ...declaredOutput(schemaHash, "draft"),
    "  - id: review",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Judge the draft you were handed.",
    "    depends_on: [implement]",
    "    inputs:",
    "      - name: draft",
    "        from:",
    "          node: implement",
    "          output: draft",
    ...declaredOutput(schemaHash, "findings"),
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: "v3-read",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "v3-model",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);

  const started = await page.request.post(`${api}/runs`, {
    data: {
      workflow_format_version: 2,
      run_id: "v3/the-read-one",
      workflow_revision_hash: revisionHash,
      agent_bindings: [
        {
          role: "builder",
          agent_configuration_revision_hash: (await configuration.json())
            .agent_configuration_revision_hash
        }
      ]
    }
  });
  expect(started.status()).toBe(201);
  const reference = (await started.json()).public_run_reference as string;

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    expect((await read.json()).state).toBe("COMPLETED");
  }).toPass({ timeout: 15_000 });

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { level: 1, name: "the chain the operator read" })).toBeVisible();
  await expect(page.getByText("v3/the-read-one")).toHaveCount(0);
  await page.getByRole("button", { name: "Run id" }).click();
  await expect(page.getByText("v3/the-read-one")).toBeVisible();

  await page.getByRole("button", { name: /implement/ }).click();
  await expect(page.getByRole("region", { name: "Prompt" })).toContainText(
    "Write three German sentences about code review."
  );
  await expect(page.getByRole("region", { name: "Output" })).toContainText("V3 provider bytes");
  await expect(page.getByLabel("What finished")).toContainText("implement");
  await expect(page.getByLabel("What finished")).not.toContainText("V3 provider bytes");
  const who = page.getByRole("region", { name: "Who" });
  await expect(who.getByText("Declared model")).toBeVisible();
  await expect(who.getByText("v3-model")).toBeVisible();
  await expect(who.getByText("Resolved model")).toBeVisible();
  await expect(who.getByText("not recorded", { exact: true })).toHaveCount(2);
  await expect(page.getByText(/not recorded yet/)).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.screenshot({ path: "test-results/v3-node-detail.png", fullPage: true });
});

test("opening Details on a saved V3 workflow shows each node with its role and instruction start", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const workflowYaml = [
    "format_version: 3",
    "name: Implement a candidate, then review it for defects",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Implement every acceptance sentence of the bound story.",
    "  - id: review",
    "    type: agent",
    "    role: reviewer",
    "    mode: headless",
    "    instruction: Name every defect with the sentence it violates.",
    "    depends_on: [implement]",
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);

  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: "Saved workflow", exact: true }).check();
  await expect(
    page.getByRole("radio", { name: /Implement a candidate, then review it for defects/ })
  ).toBeVisible();
  const row = page.getByRole("article", {
    name: "Implement a candidate, then review it for defects"
  });
  await row.getByText("Details", { exact: true }).click();
  const details = row.locator("details.revision-details");
  await expect(details).toContainText("implement");
  await expect(details).toContainText("builder");
  await expect(details).toContainText("Implement every acceptance sentence of the bound story.");
  await expect(details).toContainText("review");
  await expect(details).toContainText("reviewer");
  await expect(details).toContainText("Name every defect with the sentence it violates.");
  await page.screenshot({
    path: "test-results/v3-picker-node-previews.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/v3-picker-node-previews-mobile.png",
    fullPage: true
  });
});

test("a declared order is a material field on start, and the typed value travels as that order", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const schema = await page.request.post(`${api}/schema-revisions`, {
    headers: { "content-type": "application/json" },
    data: '{"type":"object","properties":{"portions":{"type":"integer","minimum":1}},"required":["portions"],"additionalProperties":false}'
  });
  expect([200, 201]).toContain(schema.status());
  const schemaHash = (await schema.json()).schema_revision_hash as string;

  const auth = await page.request.post(`${api}/auth-profile-revisions`, {
    data: {
      profile_id: "cook-order",
      revision_number: 1,
      provider_id: "e2e-v3",
      auth_mode: "subscription"
    }
  });
  expect(auth.status()).toBe(201);
  const configuration = await page.request.post(`${api}/agent-configuration-revisions`, {
    data: {
      model: "cook-sonnet",
      auth_profile_revision_hash: (await auth.json()).auth_profile_revision_hash,
      executor_revision: "immediate/v1",
      requested_capability: "headless"
    }
  });
  expect(configuration.status()).toBe(201);

  const answerSchemaHash = await anyJsonSchema(page);
  const workflowYaml = [
    "format_version: 3",
    "name: Cook to order",
    "graph_inputs:",
    "  - name: portions",
    "    schema:",
    "      ref: portions-schema",
    `      revision: ${schemaHash}`,
    "nodes:",
    "  - id: cook",
    "    type: agent",
    "    role: cook",
    "    mode: headless",
    "    instruction: Cook exactly what the order says.",
    "    inputs:",
    "      - name: portions",
    "        from:",
    "          graph_input: portions",
    ...declaredOutput(answerSchemaHash),
    ""
  ].join("\n");
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: workflowYaml
  });
  expect(published.status()).toBe(201);

  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: "Saved workflow" }).check();
  const workflow = page.getByRole("radio", { name: /Cook to order/ });
  await workflow.click();
  await expect(workflow).toBeChecked();

  const order = page.getByRole("article", { name: "Order portions" });
  await expect(order).toBeVisible();
  await expect(order).toContainText(`portions-schema@${schemaHash}`);
  const material = order.getByRole("textbox", { name: "Material portions" });
  await expect(material).toHaveValue("");
  await expect(page.getByRole("article", { name: /^Order / })).toHaveCount(1);

  await page.getByRole("button", { name: "Start" }).click();
  await expect(order.getByRole("alert")).toContainText(
    "input 'portions' was refused: missing"
  );
  await page.screenshot({ path: "test-results/v3-material-missing-desktop.png", fullPage: true });

  await material.fill('{"portions": 7}');
  const binding = page.getByRole("article", { name: "Binding cook" });
  const picker = binding.getByLabel("Agent for cook");
  await expect(picker).toContainText("e2e-v3 · cook-sonnet · Subscription");
  await picker.selectOption({ label: "e2e-v3 · cook-sonnet · Subscription" });

  const started: { orders: Array<{ name: string; value: string }> | null } = {
    orders: null
  };
  await page.route("**/runs", async (route) => {
    const request = route.request();
    if (request.method() === "POST" && /\/runs$/.test(new URL(request.url()).pathname)) {
      const body = request.postDataJSON() as {
        orders?: Array<{ name: string; value: string }>;
      };
      started.orders = body.orders ?? null;
    }
    await route.continue();
  });
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Cook to order" })).toBeVisible({
    timeout: 20_000
  });
  expect(started.orders).toEqual([{ name: "portions", value: '{"portions": 7}' }]);
  await page.screenshot({ path: "test-results/v3-material-started-desktop.png", fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/new");
  const mobileWorkflow = page.getByRole("radio", { name: /Cook to order/ });
  await mobileWorkflow.click();
  await expect(mobileWorkflow).toBeChecked();
  await expect(page.getByRole("article", { name: "Order portions" })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/v3-material-390x844.png", fullPage: true });
});

test("two revisions of one lineage are one picker row; the older choice changes startability", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const lineageName = "lineage-grouping-271";
  const schemaHash = await anyJsonSchema(page);
  const olderYaml = [
    "format_version: 3",
    `name: ${lineageName}`,
    "description: The first admitted member.",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write the first admitted draft.",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");
  const newestYaml = [
    "format_version: 3",
    `name: ${lineageName}`,
    "description: The catalog head.",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write the later admitted draft.",
    ""
  ].join("\n");

  const olderPublished = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: olderYaml
  });
  expect(olderPublished.status()).toBe(201);
  const olderHash = (await olderPublished.json()).workflow_revision_hash as string;
  const newestPublished = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: newestYaml
  });
  expect(newestPublished.status()).toBe(201);
  const newestHash = (await newestPublished.json()).workflow_revision_hash as string;

  const founded = await page.request.post(`${api}/workflow-lineages`, {
    data: {
      workflow_revision_hash: olderHash,
      actor: "e2e",
      activated_at: "2026-08-17T00:00:00Z"
    }
  });
  expect(founded.status()).toBe(201);
  const lineageId = (await founded.json()).lineage_id as string;
  const admitted = await page.request.post(`${api}/workflow-lineages/${lineageId}/members`, {
    data: {
      workflow_revision_hash: newestHash,
      actor: "e2e",
      activated_at: "2026-08-17T00:00:01Z"
    }
  });
  expect(admitted.status()).toBe(201);
  const head = await page.request.get(`${api}/workflow-revisions/by-name/${lineageName}`);
  expect(head.status()).toBe(200);
  expect((await head.json()).workflow_revision_hash).toBe(newestHash);

  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: "Saved workflow" }).check();
  const row = page.getByRole("article", { name: lineageName });
  await expect(row.getByRole("radio")).toHaveCount(1);
  await expect(row.getByRole("radio")).toBeDisabled();
  await expect(row).toContainText("The catalog head.");
  await expect(row).toContainText("Cannot be started");
  await expect(row).toContainText("Add one outputs: entry");
  await expect(row).not.toContainText("agent-output-shape-unavailable");
  await expect(row).not.toContainText("The first admitted member.");
  await row.getByText("Details", { exact: true }).click();
  await expect(row.getByRole("heading", { name: "Revisions" })).toBeVisible();
  await row.getByLabel(`Revision of ${lineageName}`).selectOption({ label: "Earlier" });
  await expect(row.getByRole("radio")).toBeEnabled();
  await expect(row).toContainText("The first admitted member.");
  await expect(row).not.toContainText("Cannot be started");

  const details = row.locator("details.revision-details");
  await expect(details).toContainText("Write the first admitted draft.");
  await expect(details).not.toContainText(olderHash);
  await details.getByRole("button", { name: "Workflow revision" }).click();
  await expect(details).toContainText(olderHash);
  await expect(details).not.toContainText(newestHash);

  await page.screenshot({
    path: "test-results/v3-picker-lineage-desktop.png",
    fullPage: true
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(row.getByRole("radio")).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({
    path: "test-results/v3-picker-lineage-390x844.png",
    fullPage: true
  });
});

test("Needs you names a run that is waiting for a person, by its catalog name", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const runId = "studio/waiting-inbox";
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: Waiting in the studio",
      "nodes:",
      "  - id: ask",
      "    type: wait",
      "    prompt: Approve this, or name the blocking defect.",
      ...declaredOutput(schemaHash, "approval"),
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const started = await page.request.post(`${api}/runs`, {
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
    const listed = await page.request.get(`${api}/runs?state=WAITING_INPUT&limit=50`);
    expect(listed.status()).toBe(200);
    const body = await listed.json();
    expect(body.items.some((item: { run_id: string }) => item.run_id === runId)).toBe(true);
  }).toPass({ timeout: 15_000 });

  await page.goto("/atelier");
  // This backend is shared across every earlier test in this file, so other
  // runs may already sit in Needs you: this run is named, not counted.
  const needsYou = page.getByRole("region", { name: /^Needs you/ });
  const row = needsYou.getByRole("link", { name: /Waiting in the studio/ });
  await expect(row).toBeVisible();
  await expect(row).toContainText("Answer");
  const boardLink = page.getByRole("navigation", { name: "Workshop" }).getByRole("link", { name: /Board/ });
  await expect(boardLink).toContainText(/[1-9]/);

  await page.screenshot({ path: "test-results/studio-inbox-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(row).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/studio-inbox-390x844.png", fullPage: true });

  await row.click();
  await expect(page).toHaveURL(new RegExp(`/atelier/runs/${reference.replace(".", "\\.")}$`));
});

test("publishing a V3 workflow from the cockpit names it so by-name answers", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const lineageName = "name-admission-213";
  const schemaHash = await anyJsonSchema(page);
  const yaml = [
    "format_version: 3",
    `name: ${lineageName}`,
    "description: Named by the cockpit after publish.",
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Write the admitted draft.",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");

  await page.goto("/atelier/new");
  await page.getByLabel("Publish YAML").check();
  await page.getByLabel("Exact workflow YAML").fill(yaml);
  await page.getByRole("button", { name: "Review publication" }).click();
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Run ID" })).toBeVisible({
    timeout: 20_000
  });

  const head = await page.request.get(`${api}/workflow-revisions/by-name/${lineageName}`);
  expect(head.status()).toBe(200);
  const named = await head.json();
  expect(named.display_name).toBe(lineageName);
  expect(named.lineage_id).toMatch(/^[0-9a-f]{64}$/);
  expect(named.workflow_revision_hash).toMatch(/^[0-9a-f]{64}$/);

  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: "Saved workflow" }).check();
  const row = page.getByRole("article", { name: lineageName });
  await expect(row.getByRole("radio")).toBeVisible();
  await expect(row).not.toContainText("Unlisted");
  await expect(row).not.toContainText("Unnamable");
  await page.screenshot({
    path: "test-results/v3-picker-named-after-publish-desktop.png",
    fullPage: true
  });
});

test("a published name the catalog does not hold is named, not silent", async ({
  page
}) => {
  const api = "/atelier/api/v1";
  const unlisted = "unlisted-213";
  const unnamable = "Der erste Lauf auf 213";
  const schemaHash = await anyJsonSchema(page);
  const unlistedYaml = [
    "format_version: 3",
    `name: ${unlisted}`,
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: Published and never admitted.",
    ...declaredOutput(schemaHash),
    ""
  ].join("\n");
  const unnamableYaml = [
    "format_version: 3",
    `name: ${unnamable}`,
    "nodes:",
    "  - id: implement",
    "    type: agent",
    "    role: builder",
    "    mode: headless",
    "    instruction: This title cannot be a catalog name.",
    ""
  ].join("\n");

  expect(
    (await page.request.post(`${api}/workflow-revisions`, {
      headers: { "content-type": "application/yaml" },
      data: unlistedYaml
    })).status()
  ).toBe(201);
  expect(
    (await page.request.post(`${api}/workflow-revisions`, {
      headers: { "content-type": "application/yaml" },
      data: unnamableYaml
    })).status()
  ).toBe(201);

  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: "Saved workflow" }).check();
  await expect(page.getByRole("article", { name: unlisted })).toContainText("Unlisted");
  await expect(page.getByRole("article", { name: unnamable })).toContainText("Unnamable");
  await page.screenshot({
    path: "test-results/v3-picker-unlisted-unnamable-desktop.png",
    fullPage: true
  });
});

test("a waiting V3 run is answerable on its own run page", async ({ page }) => {
  const api = "/atelier/api/v1";
  const schemaHash = await anyJsonSchema(page);
  const runId = "v3/answer-card";
  const published = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: [
      "format_version: 3",
      "name: answer-card-194",
      "nodes:",
      "  - id: ask",
      "    type: wait",
      "    prompt: Approve this, or name the blocking defect.",
      ...declaredOutput(schemaHash, "approval"),
      ""
    ].join("\n")
  });
  expect(published.status()).toBe(201);
  const revisionHash = (await published.json()).workflow_revision_hash as string;

  const started = await page.request.post(`${api}/runs`, {
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
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    expect((await read.json()).state).toBe("WAITING_INPUT");
  }).toPass({ timeout: 15_000 });

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { level: 1, name: "answer-card-194" })).toBeVisible();
  await expect(page.getByText(runId)).toHaveCount(0);
  await page.getByRole("button", { name: "Run id" }).click();
  await expect(page.getByText(runId)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Answer needed" })).toBeVisible();
  await expect(page.getByText("Approve this, or name the blocking defect.")).toBeVisible();
  const card = page.getByRole("region", { name: "Answer needed" });
  await card.getByRole("textbox", { name: "Answer" }).fill("true");
  await card.getByRole("button", { name: "Answer" }).click();
  await expect(page.getByRole("heading", { name: /Answer pending|Answer needed|Answer uncertain/ })).toBeVisible();

  await expect(async () => {
    const read = await page.request.get(`${api}/runs/${reference}`);
    expect(read.status()).toBe(200);
    const body = await read.json();
    expect(body.state).toBe("COMPLETED");
    expect(body.run_id).toBe(runId);
    expect(body.terminal_hash).toMatch(/^[0-9a-f]{64}$/);
  }).toPass({ timeout: 20_000 });

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { name: "Answer needed" })).toHaveCount(0);
  await expect(page.getByText(/not yet/)).toHaveCount(0);

  await page.screenshot({
    path: "test-results/v3-answer-card-desktop.png",
    fullPage: true
  });
});
