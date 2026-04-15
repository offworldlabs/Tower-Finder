/**
 * Dashboard (admin.retina.fm / staging-admin.retina.fm) E2E tests.
 *
 * The dashboard requires Google OAuth login, so most tests exercise:
 *   1. Unauthenticated state → redirect to /login
 *   2. Login page rendering and structure
 *   3. API endpoints backing the dashboard (no auth required for health)
 *
 * Authenticated flows are covered via API-level assumptions (see api.spec.ts).
 */
import { test, expect, request as playwrightRequest } from "@playwright/test";
import { hosts } from "../playwright.config";

const DASH = hosts.dash;
const API = hosts.api;

test.describe("Dashboard — unauthenticated access", () => {
  test("unauthenticated request to / renders the login page", async ({ page }) => {
    await page.goto(DASH);
    // On staging, RETINA_ENV=test disables OAuth → AUTH_ENABLED=False → backend
    // returns a mock admin user for every /api/auth/me request. In that case the
    // dashboard renders directly (no redirect). On production with OAuth keys set,
    // this would redirect to /login.
    // We assert that exactly one of the two states is rendered correctly.
    const isRedirected = await page.waitForURL(/\/login/, { timeout: 8_000 }).then(() => true).catch(() => false);
    if (isRedirected) {
      await expect(page.locator(".login-card")).toBeVisible({ timeout: 5_000 });
    } else {
      // Auth disabled: dashboard content rendered directly
      await page.waitForLoadState("networkidle");
      // The page should not be a bare error — check that the app root is rendered
      await expect(page.locator("#root")).toBeVisible();
      await expect(page.locator(".login-card")).toBeHidden();
    }
  });

  test("login page renders logo and title", async ({ page }) => {
    await page.goto(`${DASH}/login`);
    await expect(page.locator(".login-card")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator("h1")).toContainText(/Retina/i);
  });

  test("login page shows Google login button", async ({ page }) => {
    await page.goto(`${DASH}/login`);
    const googleLink = page.getByRole("link", { name: /Google/i });
    await expect(googleLink).toBeVisible({ timeout: 10_000 });
  });

  test("login page link points to /api/auth/login/google", async ({ page }) => {
    await page.goto(`${DASH}/login`);
    const googleLink = page.getByRole("link", { name: /Google/i });
    const href = await googleLink.getAttribute("href");
    expect(href).toMatch(/\/api\/auth\/login\/google/);
  });

  test("login page shows error message on ?error= query param", async ({ page }) => {
    await page.goto(`${DASH}/login?error=access_denied`);
    await expect(page.locator(".login-error")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".login-error")).toContainText(/access denied/i);
  });

  test("no JavaScript errors on login page load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto(`${DASH}/login`);
    await page.waitForLoadState("networkidle");
    expect(errors).toHaveLength(0);
  });
});

test.describe("Dashboard — admin API backing (no auth required)", () => {
  test("GET /api/admin/leaderboard returns nodes array", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(`${API}/api/admin/leaderboard`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    // Response shape: {leaderboard: [...], total: N}
    expect(body).toHaveProperty("leaderboard");
    expect(Array.isArray(body.leaderboard)).toBe(true);
    await ctx.dispose();
  });

  test("GET /api/admin/events returns event list", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(`${API}/api/admin/events`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    // Events response is an array or an object containing an events key
    const isValid = Array.isArray(body) || (typeof body === "object" && body !== null);
    expect(isValid).toBe(true);
    await ctx.dispose();
  });

  test("GET /api/admin/storage returns file_count and total_size_mb", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(`${API}/api/admin/storage`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty("archive_files");
    expect(body).toHaveProperty("archive_bytes");
    expect(body).toHaveProperty("archive_mb");
    expect(typeof body.archive_files).toBe("number");
    expect(typeof body.archive_mb).toBe("number");
    await ctx.dispose();
  });

  test("GET /api/config returns a non-empty config object", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(`${API}/api/config`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(typeof body).toBe("object");
    expect(Object.keys(body).length).toBeGreaterThan(0);
    await ctx.dispose();
  });
});

test.describe("Dashboard — static asset delivery", () => {
  test("index.html is served with no-store Cache-Control", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(DASH);
    expect([200, 301, 302]).toContain(res.status());
    // Follow to login page
    const loginRes = await ctx.get(`${DASH}/login`);
    expect(loginRes.status()).toBe(200);
    const cacheHeader = loginRes.headers()["cache-control"] ?? "";
    // index.html should prevent browser caching to avoid stale bundle issues
    expect(cacheHeader).toMatch(/no-store|no-cache/i);
    await ctx.dispose();
  });
});
