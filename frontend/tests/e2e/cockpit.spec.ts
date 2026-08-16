import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

const foundReference = "run1.Zm91bmQtcnVu";
const absentReference = "run1.YWJzZW50LXJ1bg";

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

  await page.getByLabel("Profile ID").fill("local");
  await page.getByLabel("Revision").fill("1");
  await page.getByLabel("Provider").fill("e2e");
  await page.getByLabel("Auth mode").selectOption("subscription");
  await page.getByLabel("Model").fill("test-model");
  await page.getByLabel("Executor").fill("blocking/v1");
  const binding = page.getByRole("article", { name: "Binding builder" });
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
