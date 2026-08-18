import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

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
  return (await published.json()).revision_hash as string;
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
  await expect(page.getByRole("heading", { name: "Studio" })).toBeVisible();

  const rail = page.getByRole("navigation", { name: "Workshop" });
  await expect(rail.getByRole("link", { name: "Studio" })).toBeVisible();
  await expect(rail.getByRole("link", { name: "Projekte" })).toBeVisible();
  await expect(rail.getByRole("link", { name: "Runs" })).toHaveCount(0);
  await expect(rail.getByRole("link", { name: "Library" })).toHaveCount(0);
  await expect(rail.getByRole("link", { name: "Settings" })).toHaveCount(0);
  await expect(rail.getByText("Runs", { exact: true })).toBeVisible();
  await expect(rail.getByText("Library", { exact: true })).toBeVisible();
  await expect(rail.getByText("Settings", { exact: true })).toBeVisible();
  await expect(rail.locator("[title*='REQ-UI-13']")).toBeVisible();
  await expect(page.getByRole("banner").getByText("atelier")).toBeVisible();
  await expect(page.getByRole("banner").getByRole("button", { name: /This workshop/ })).toBeVisible();

  await rail.getByRole("link", { name: "Projekte" }).click();
  await expect(page.getByRole("heading", { name: "This workshop" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/project$/);

  const stillOnProject = page.url();
  await rail.getByText("Library", { exact: true }).click();
  await expect(page).toHaveURL(stillOnProject);

  await rail.getByRole("link", { name: "Studio" }).click();
  await expect(page.getByRole("heading", { name: "Studio" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier$/);

  await page.screenshot({ path: "test-results/shell-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("navigation", { name: "Workshop" })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/shell-390x844.png", fullPage: true });
});

test("publishes, binds, and starts one visible V2 Agent", async ({ page }) => {
  await page.goto("/atelier/new");
  await page.getByLabel("Publish YAML").check();
  await page.getByLabel("Exact workflow YAML").fill("format_version: 2\nstart: build\nnodes:\n  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}\n  - {id: build, type: agent, role: builder, job: prove-heartbeat, next: done}\n");
  await page.getByRole("button", { name: "Review publication" }).click();
  await expect(page.getByRole("dialog", { name: "Publish this exact workflow?" })).toBeVisible();
  await page.screenshot({ path: "test-results/v2-publish-review-desktop.png", fullPage: true });
  let continuePublication = (): void => {};
  const publicationGate = new Promise<void>((resolve) => { continuePublication = resolve; });
  await page.route("**/workflow-revisions", async (route) => { await publicationGate; await route.continue(); });
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Publishing workflow");
  await page.screenshot({ path: "test-results/v2-publishing-desktop.png", fullPage: true });
  continuePublication();
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByText("Complete every field.")).toBeVisible();
  await page.screenshot({ path: "test-results/v2-bindings-error-desktop.png", fullPage: true });

  const binding = page.getByRole("article", { name: "Binding builder" });
  await binding.locator("summary").click();
  await page.getByLabel("Profile ID").fill("local");
  await page.getByLabel("Revision").fill("1");
  await page.getByLabel("Provider").fill("e2e");
  await page.getByLabel("Auth mode").selectOption("subscription");
  await page.getByLabel("Model").fill("test-model");
  await page.getByLabel("Executor").fill("blocking/v1");
  await expect(page.getByText("Complete every field.")).toHaveCount(0);
  await page.screenshot({ path: "test-results/v2-bindings-corrected-desktop.png", fullPage: true });
  let continueAuth = (): void => {};
  const authGate = new Promise<void>((resolve) => { continueAuth = resolve; });
  await page.route("**/auth-profile-revisions", async (route) => { await authGate; await route.continue(); });
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByRole("status")).toContainText("Starting the exact run");
  await expect(binding).toHaveClass(/node-working/);
  await expect(page.getByRole("button", { name: "Start" })).toBeDisabled();
  await assertContrastAtLeast(page.getByRole("button", { name: "Start" }), 4.5);
  await page.screenshot({ path: "test-results/v2-bindings-loading-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);
  continueAuth();
  const working = page.getByRole("article", { name: "build — Working" });
  await expect(working).toContainText("e2e · test-model");
  await expect(working).toContainText("Subscription · blocking/v1");
  await expect(async () => {
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(working).toContainText("possibly ran");
  }).toPass();
  await page.screenshot({ path: "test-results/v2-working-desktop.png", fullPage: true });

  const completed = page.getByRole("article", { name: "build — Done" });
  await expect(async () => {
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(completed).toBeVisible({ timeout: 500 });
  }).toPass({ timeout: 8_000 });
  await expect(completed).toContainText("Grüße 東京");
  await expect(completed).toContainText("14 bytes");
  await expect(completed).toContainText("Verified");
  await page.screenshot({ path: "test-results/v2-completed-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/v2-completed-390x844.png", fullPage: true });

  await page.reload();
  await expect(page.getByRole("alert")).toContainText("Output mismatch");
  await expect(page.getByRole("region", { name: "Verified output" })).toHaveCount(0);
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/v2-output-mismatch-390x844.png", fullPage: true });

  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Studio" })).toBeVisible();
  const workshop = page.getByRole("article", { name: "This workshop" }).getByRole("link");
  await expect(workshop).toBeVisible();
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({ path: "test-results/studio-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await assertMobileSurface(page);
  await assertNoSeriousAccessibilityFindings(page);
  await page.screenshot({ path: "test-results/studio-390x844.png", fullPage: true });
  await workshop.click();
  await expect(page.getByRole("heading", { name: "This workshop" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Queue" })).toBeVisible();
  await page.screenshot({ path: "test-results/project-390x844.png", fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({ path: "test-results/project-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);


});


/**
 * A click never asks the server for a page, so the project level looked right
 * while a reload of it answered 404. This walks the way an operator arrives from
 * outside — the pasted link — and then reloads the level he is standing on.
 */
test("opens the project level from a cold link and survives a reload", async ({ page }) => {
  await page.goto("/atelier/project");
  await expect(page.getByRole("heading", { name: "This workshop" })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: "This workshop" })).toBeVisible();
  await expect(page).toHaveURL(/\/atelier\/project$/);
});

test("walks the whole workshop: studio into the project, project into the run, and the trail back up", async ({ page }) => {
  await page.goto("/atelier");
  await expect(page.getByRole("heading", { name: "Studio" })).toBeVisible();
  await page.getByRole("article", { name: "This workshop" }).getByRole("link").click();
  await expect(page.getByRole("heading", { name: "This workshop" })).toBeVisible();

  await page
    .getByRole("region", { name: "Waiting for you" })
    .getByRole("link")
    .first()
    .click();
  await expect(page.getByRole("heading", { name: /^Run / })).toBeVisible();
  const trail = page.getByRole("navigation", { name: "Where you are" });
  await expect(trail.getByRole("link", { name: "Studio" })).toBeVisible();
  await expect(trail.getByRole("link", { name: "This workshop" })).toBeVisible();
  await page.screenshot({ path: "test-results/run-trail-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);

  await trail.getByRole("link", { name: "This workshop" }).click();
  await expect(page.getByRole("heading", { name: "This workshop" })).toBeVisible();
  await page
    .getByRole("navigation", { name: "Where you are" })
    .getByRole("link", { name: "Studio" })
    .click();
  await expect(page.getByRole("heading", { name: "Studio" })).toBeVisible();
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
  const foundEvidence = page.getByLabel("Evidence");
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
  const absentEvidence = page.getByLabel("Evidence");
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
    "button, input[type=text], textarea, .determination-picker label, summary"
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

async function assertContrastAtLeast(control: Locator, minimum: number): Promise<void> {
  const [foreground, background, effectiveOpacity] = await control.evaluate((element) => {
    const style = getComputedStyle(element);
    let opacity = 1;
    for (let current: Element | null = element; current !== null; current = current.parentElement) {
      opacity *= Number(getComputedStyle(current).opacity);
    }
    return [style.color, style.backgroundColor, opacity] as const;
  });
  expect(effectiveOpacity).toBe(1);
  const luminance = (color: string): number => {
    const channels = color.match(/[\d.]+/g)?.slice(0, 3).map(Number) ?? [];
    const linear = channels.map((value) => {
      const ratio = value / 255;
      return ratio <= 0.04045 ? ratio / 12.92 : ((ratio + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * linear[0]! + 0.7152 * linear[1]! + 0.0722 * linear[2]!;
  };
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  expect((values[0]! + 0.05) / (values[1]! + 0.05)).toBeGreaterThanOrEqual(minimum);
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
  const revisionHash = (await published.json()).revision_hash as string;

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

  await page.goto(`/atelier/runs/${reference}`);

  await expect(page.getByRole("heading", { level: 1, name: "Run v3/seen-in-the-browser" })).toBeVisible();
  const graph = page.getByRole("region", { name: "Workflow" });
  await expect(graph.getByRole("button", { name: "implement — Done" })).toBeVisible();
  await expect(graph.getByRole("button", { name: "review — Done" })).toBeVisible();
  await expect(page.getByLabel("Where this run stands")).toContainText("Done");
  await expect(page.getByLabel("Where this run stands")).not.toContainText("Snapshot");
  await expect(page.getByText(terminal as unknown as string)).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);

  await page.screenshot({ path: "test-results/v3-run-desktop.png", fullPage: true });
  await page.screenshot({ path: "test-results/v3-graph-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { level: 1, name: "Run v3/seen-in-the-browser" })).toBeVisible();
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
  await expect(page.getByRole("heading", { level: 1, name: /^Run / })).toBeVisible();
  await expect(page.getByLabel("Where this run stands")).toContainText("Done", {
    timeout: 20_000
  });
  await page.screenshot({ path: "test-results/named-agent-run-desktop.png", fullPage: true });
  await assertNoSeriousAccessibilityFindings(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { level: 1, name: /^Run / })).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/named-agent-run-390x844.png", fullPage: true });
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

  await expect(page.getByRole("heading", { level: 1, name: /^Run / })).toBeVisible();
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
  const revisionHash = (await published.json()).revision_hash as string;

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

  const arriving = page.getByRole("list", { name: "Events as they arrive" });
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
  const revisionHash = (await published.json()).revision_hash as string;

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
  await expect(graph.getByRole("button", { name: /Working$/ })).toBeVisible({ timeout: 10_000 });
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
  const revisionHash = (await published.json()).revision_hash as string;

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
    expect(body.state).toBe("STARTED");
    expect(body.current_node_id).toBe("implement");
  }).toPass({ timeout: 15_000 });

  await page.goto(`/atelier/runs/${reference}`);
  await expect(page.getByRole("heading", { level: 1, name: "Run v3/the-silent-one" })).toBeVisible();
  await expect(page.getByLabel("Where this run stands")).not.toContainText("Done");

  await page.getByRole("button", { name: /implement/ }).click();
  // Nothing was written, so there is nothing to show as an answer -- and the
  // panel says so honestly rather than dressing the silence as a value.
  await expect(page.getByLabel("Asked")).toContainText(
    "Write three German sentences about code review."
  );
  await expect(page.getByLabel("Answered")).toContainText("Nothing written yet.");
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
  const revisionHash = (await published.json()).revision_hash as string;

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
  await expect(page.getByRole("heading", { level: 1, name: "Run v3/the-read-one" })).toBeVisible();

  await page.getByRole("button", { name: /implement/ }).click();
  await expect(page.getByLabel("Asked")).toContainText(
    "Write three German sentences about code review."
  );
  await expect(page.getByLabel("Answered")).toContainText("V3 provider bytes");
  await expect(page.getByLabel("Events as they arrive")).toContainText(
    "V3 provider bytes"
  );
  await expect(page.getByText(/not recorded yet/)).toBeVisible();
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
  const revisionHash = (await published.json()).revision_hash as string;

  await page.goto("/atelier/new");
  await page.getByLabel("Saved workflow").check();
  await expect(
    page.getByRole("radio", { name: /Implement a candidate, then review it for defects/ })
  ).toBeVisible();
  const details = page.locator("details.revision-details").filter({ hasText: revisionHash });
  await details.getByText("Details", { exact: true }).click();
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
  const schemaHash = (await schema.json()).revision_hash as string;

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
  await page.getByRole("radio", { name: /Cook to order/ }).check();

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
  await expect(page.getByRole("heading", { level: 1, name: /^Run / })).toBeVisible({
    timeout: 20_000
  });
  expect(started.orders).toEqual([{ name: "portions", value: '{"portions": 7}' }]);
  await page.screenshot({ path: "test-results/v3-material-started-desktop.png", fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/atelier/new");
  await page.getByRole("radio", { name: /Cook to order/ }).check();
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
  const olderHash = (await olderPublished.json()).revision_hash as string;
  const newestPublished = await page.request.post(`${api}/workflow-revisions`, {
    headers: { "content-type": "application/yaml" },
    data: newestYaml
  });
  expect(newestPublished.status()).toBe(201);
  const newestHash = (await newestPublished.json()).revision_hash as string;

  const founded = await page.request.post(`${api}/workflow-lineages`, {
    data: {
      revision_hash: olderHash,
      actor: "e2e",
      activated_at: "2026-08-17T00:00:00Z"
    }
  });
  expect(founded.status()).toBe(201);
  const lineageId = (await founded.json()).lineage_id as string;
  const admitted = await page.request.post(`${api}/workflow-lineages/${lineageId}/members`, {
    data: {
      revision_hash: newestHash,
      actor: "e2e",
      activated_at: "2026-08-17T00:00:01Z"
    }
  });
  expect(admitted.status()).toBe(201);
  const head = await page.request.get(`${api}/workflow-revisions/by-name/${lineageName}`);
  expect(head.status()).toBe(200);
  expect((await head.json()).revision_hash).toBe(newestHash);

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
  await expect(row.getByLabel(`Revisions of ${lineageName}`)).toBeVisible();

  await row.getByText("Revisions", { exact: true }).click();
  await row.getByLabel(`Revision of ${lineageName}`).selectOption({ label: "Earlier" });
  await expect(row.getByRole("radio")).toBeEnabled();
  await expect(row).toContainText("The first admitted member.");
  await expect(row).not.toContainText("Cannot be started");

  await row.getByText("Details", { exact: true }).click();
  const details = row.locator("details.revision-details");
  await expect(details).toContainText("Write the first admitted draft.");
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

test("the studio inbox names a run that is waiting for a person", async ({ page }) => {
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
  const revisionHash = (await published.json()).revision_hash as string;

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
  const inbox = page.getByRole("region", { name: "Waiting for you" });
  const row = inbox.getByRole("link", { name: new RegExp(runId) });
  await expect(row).toBeVisible();
  await expect(row).toContainText("Answer");
  const card = page.getByRole("article", { name: "This workshop" });
  await expect(card).toContainText("waiting for you");

  await page.screenshot({ path: "test-results/studio-inbox-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(row).toBeVisible();
  await assertMobileSurface(page);
  await page.screenshot({ path: "test-results/studio-inbox-390x844.png", fullPage: true });

  await row.click();
  await expect(page).toHaveURL(new RegExp(`/atelier/runs/${reference.replace(".", "\\.")}$`));
});

