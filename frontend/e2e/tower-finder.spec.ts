/**
 * Tower Finder frontend E2E tests.
 * Tests the main retina.fm / staging.retina.fm interface:
 * - Page load and header rendering
 * - Tab navigation
 * - Search form validation and submission
 * - Results table rendering
 * - Map rendering
 */
import { test, expect } from "@playwright/test";
import { hosts } from "../playwright.config";

const BASE = hosts.frontend;

test.describe("Tower Finder — page load", () => {
  test("loads and renders the app header", async ({ page }) => {
    await page.goto(BASE);
    await expect(page).toHaveTitle(/Tower Finder|RETINA/i);
    await expect(page.locator("h1")).toBeVisible();
  });

  test("renders Tower Search tab by default on main domain", async ({ page }) => {
    await page.goto(BASE);
    // On the tower finder domain, Tower Search tab should be present and active
    const tab = page.getByRole("button", { name: /Tower Search/i });
    await expect(tab).toBeVisible();
    await expect(tab).toHaveClass(/active/);
  });

  test("search form is visible with lat/lon/altitude inputs", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.getByLabel(/latitude/i)).toBeVisible();
    await expect(page.getByLabel(/longitude/i)).toBeVisible();
    await expect(page.getByLabel(/altitude/i)).toBeVisible();
  });

  test("no JavaScript errors on load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForLoadState("networkidle");
    expect(errors).toHaveLength(0);
  });
});

test.describe("Tower Finder — search form", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
  });

  test("shows validation error if search submitted with empty fields", async ({ page }) => {
    const btn = page.locator("button[type='submit']").filter({ hasText: /Find Towers/i });
    await btn.click();
    // Either HTML5 validation (field required) or custom error message
    const latInput = page.getByLabel(/latitude/i);
    const validationMsg = await latInput.evaluate((el: HTMLInputElement) => el.validationMessage);
    expect(validationMsg).not.toBe("");
  });

  test("auto-detects US source for US coordinates", async ({ page }) => {
    await page.getByLabel(/latitude/i).fill("37.7749");
    await page.getByLabel(/longitude/i).fill("-122.4194");
    // Source dropdown should switch to "us" — toHaveValue auto-waits for the useEffect
    const sourceSelect = page.getByLabel(/source|country|region/i).first();
    if (await sourceSelect.isVisible()) {
      await expect(sourceSelect).toHaveValue("us");
    }
  });

  test("auto-fetches elevation when lat/lon are entered", async ({ page }) => {
    // Set up response interceptor before triggering the network request
    const elevationResponse = page
      .waitForResponse((r) => r.url().includes("elevation"), { timeout: 5_000 })
      .catch(() => null); // resolves null if no elevation request fires

    await page.getByLabel(/latitude/i).fill("37.7749");
    await page.getByLabel(/longitude/i).fill("-122.4194");

    const gotElevation = await elevationResponse;
    const altVal = await page.getByLabel(/altitude/i).inputValue();
    // Either the elevation field was populated or an elevation API call was made
    const hasResult = altVal !== "" || gotElevation !== null;
    expect(hasResult).toBe(true);
  });

  test("frequency filter toggle shows/hides frequency inputs", async ({ page }) => {
    const toggle = page.getByRole("button", { name: /frequenc/i });
    await expect(toggle).toBeVisible(); // Fail fast if the toggle was removed from the UI

    const freqInput = page.locator("input[placeholder*='MHz']").first();
    const initiallyVisible = await freqInput.isVisible().catch(() => false);

    await toggle.click();

    // Use Playwright auto-waiting assertions instead of a fixed sleep
    if (initiallyVisible) {
      await expect(freqInput).toBeHidden();
    } else {
      await expect(freqInput).toBeVisible();
    }
  });
});

test.describe("Tower Finder — search results", () => {
  test("returns tower results for a known US location", async ({ page }) => {
    // Mock the API to avoid dependency on live FCC data
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          towers: [
            {
              callsign: "KQED",
              band: "FM",
              frequency_mhz: 88.5,
              distance_km: 12.3,
              distance_class: "Ideal",
              latitude: 37.75,
              longitude: -122.45,
              power_kw: 110,
              antenna_height_m: 440,
              rank: 1,
              name: "KQED-FM",
              state: "CA",
            },
            {
              callsign: "KCBS",
              band: "AM",
              frequency_mhz: 0.74,
              distance_km: 8.1,
              distance_class: "Ideal",
              latitude: 37.60,
              longitude: -122.38,
              power_kw: 5,
              antenna_height_m: 0,
              rank: 2,
              name: "KCBS",
              state: "CA",
            },
          ],
          query: { latitude: 37.7749, longitude: -122.4194, altitude_m: 15 },
          count: 2,
        }),
      });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("37.7749");
    await page.getByLabel(/longitude/i).fill("-122.4194");
    await page.getByLabel(/altitude/i).fill("15");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    // Results table should appear
    await expect(page.locator("table, [data-testid='results']")).toBeVisible({ timeout: 10000 });

    // Should show at least one result row
    const rows = page.locator("tbody tr");
    await expect(rows).toHaveCount(2);

    // Summary strip should show tower count
    await expect(page.locator(".summary-strip, [class*='summary']")).toBeVisible();
    await expect(page.locator(".results-count")).toHaveText("2");
  });

  test("shows no-results message when API returns empty towers", async ({ page }) => {
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ towers: [], query: { latitude: 0, longitude: 0, altitude_m: 0 }, count: 0 }),
      });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("0");
    await page.getByLabel(/longitude/i).fill("0");
    await page.getByLabel(/altitude/i).fill("10");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    await expect(page.getByText(/No suitable broadcast towers/i)).toBeVisible({ timeout: 10000 });
  });

  test("shows error banner on API failure", async ({ page }) => {
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({ status: 500, body: "Internal Server Error" });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("37.7749");
    await page.getByLabel(/longitude/i).fill("-122.4194");
    await page.getByLabel(/altitude/i).fill("15");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    await expect(page.locator(".error-banner, [class*='error']")).toBeVisible({ timeout: 10000 });
  });
});

test.describe("Tower Finder — map rendering", () => {
  test("Leaflet map container is present", async ({ page }) => {
    await page.goto(BASE);
    // TowerMap uses Leaflet — look for the leaflet container
    await expect(page.locator(".leaflet-container")).toBeVisible({ timeout: 8000 });
  });
});
