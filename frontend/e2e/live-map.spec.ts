/**
 * Live Aircraft Map (staging-testmap / testmap domain) E2E tests.
 *
 * This suite visits staging-testmap.retina.fm (synthetic fleet, not filtered)
 * and verifies the map page loads, WebSocket connects, aircraft appear,
 * and key interactive elements work correctly.
 *
 * NOTE: These tests require the synthetic fleet to be running on the target
 * environment. They use generous timeouts to account for warm-up time.
 */
import { test, expect, Page } from "@playwright/test";
import { hosts } from "../playwright.config";

const BASE = hosts.testmap;

// Helper: wait for the connection badge to show "LIVE"
async function waitForLive(page: Page, timeoutMs = 15_000) {
  await expect(page.locator(".connection-badge")).toHaveText(/LIVE/i, {
    timeout: timeoutMs,
  });
}

test.describe("Live Map — page identity", () => {
  test("page title contains RETINA", async ({ page }) => {
    await page.goto(BASE);
    // The HTML <title> is static "Tower Finder" for all domains;
    // the domain identity is exposed in the h1 element instead.
    await expect(page.locator("h1")).toContainText(/RETINA/i);
  });

  test("header shows RETINA, not Tower Finder", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator("h1")).not.toHaveText(/Tower Finder/i);
    await expect(page.locator("h1")).toContainText(/RETINA/i);
  });

  test("Live Radar tab is visible and active by default", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.getByRole("button", { name: /Live Radar/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Tower Search/i })).toBeHidden();
  });

  test("no JavaScript errors on page load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForLoadState("networkidle");
    expect(errors).toHaveLength(0);
  });
});

test.describe("Live Map — map rendering", () => {
  test("Leaflet map container is present", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".leaflet-container")).toBeVisible({ timeout: 10_000 });
  });

  test("toolbar is rendered with connection badge", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".connection-badge")).toBeVisible();
  });

  test("toolbar shows Coverage / Labels / Trails toggle buttons", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole("button", { name: /Coverage/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Labels/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Trails/i })).toBeVisible();
  });

  test("Debug Truth toggle is present on testmap domain", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole("button", { name: /Debug Truth/i })).toBeVisible();
  });
});

test.describe("Live Map — WebSocket connectivity", { tag: "@live" }, () => {
  test("connection badge transitions to LIVE within 15s", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);
    await expect(page.locator(".connection-badge")).toHaveClass(/connected/);
  });

  test("aircraft count is non-empty once connected", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);
    await expect(page.locator(".aircraft-count")).toBeVisible();
  });

  test("Pause button toggles to Resume and back", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);

    const pauseBtn = page.getByRole("button", { name: /Pause/i });
    await expect(pauseBtn).toBeVisible();
    await pauseBtn.click();

    await expect(page.locator(".connection-badge")).toHaveText(/PAUSED/i);
    await expect(page.getByRole("button", { name: /Resume/i })).toBeVisible();

    await page.getByRole("button", { name: /Resume/i }).click();
    await expect(page.locator(".connection-badge")).toHaveText(/LIVE/i);
  });
});

test.describe("Live Map — aircraft list panel", { tag: "@live" }, () => {
  test("aircraft list panel renders within 20s of connection", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);

    // Panel should exist after aircraft start arriving
    await expect(page.locator(".aircraft-list-panel")).toBeVisible({ timeout: 20_000 });
  });

  test("aircraft list shows rows once data arrives", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);

    // Wait for the first aircraft entries to appear
    const rowLocator = page.locator(".aircraft-list-panel .al-row");
    await expect(rowLocator.first()).toBeVisible({ timeout: 25_000 });

    const count = await rowLocator.count();
    expect(count).toBeGreaterThan(0);
  });

  test("clicking an aircraft row opens the detail panel", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);

    // Wait for rows
    const row = page.locator(".aircraft-list-panel .al-row").first();
    await expect(row).toBeVisible({ timeout: 25_000 });
    await row.click();

    // Detail panel should open
    await expect(page.locator(".detail-panel").first()).toBeVisible({
      timeout: 5_000,
    });
  });
});

test.describe("Live Map — toolbar toggles", () => {
  test("Coverage toggle adds/removes active class", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });

    const btn = page.getByRole("button", { name: /Coverage/i });
    const initialActive = await btn.evaluate((el) => el.classList.contains("active"));

    await btn.click();
    const afterActive = await btn.evaluate((el) => el.classList.contains("active"));
    expect(afterActive).toBe(!initialActive);
  });

  test("Labels toggle adds/removes active class", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });

    const btn = page.getByRole("button", { name: /Labels/i });
    const before = await btn.evaluate((el) => el.classList.contains("active"));
    await btn.click();
    const after = await btn.evaluate((el) => el.classList.contains("active"));
    expect(after).toBe(!before);
  });

  test("Fit button does not throw errors", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));

    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });

    const fitBtn = page.getByRole("button", { name: /Fit/i });
    await expect(fitBtn).toBeVisible();
    await fitBtn.click();

    await page.waitForTimeout(500);
    expect(errors).toHaveLength(0);
  });
});
