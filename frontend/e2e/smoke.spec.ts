import { expect, test } from "@playwright/test";

test("/ loads the app shell", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  await expect(page.locator('[data-screen="search-screen"]')).toBeVisible();
  expect(errors).toEqual([]);
});

test("/ deep links load the matching screen", async ({ page }) => {
  await page.goto("/search/dune");
  await expect(page.locator('[data-screen="search-results-screen"]')).toBeVisible();
});

test("/settings loads and lists categories", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.locator(".categories button").first()).toBeVisible();
  expect(await page.locator(".categories button").count()).toBeGreaterThan(1);
});

test("the PWA manifest, favicon, service worker and logo are served", async ({ request }) => {
  const manifest = await request.get("/manifest.json");
  expect(manifest.status()).toBe(200);
  expect((await manifest.json()).short_name).toBe("PlayBay");
  for (const path of ["/favicon.png", "/sw.js", "/lib/playbay-logo.svg"]) {
    expect((await request.get(path)).status(), path).toBe(200);
  }
});

test("unknown API paths are a JSON 404, not the app", async ({ request }) => {
  const response = await request.get("/api/does-not-exist");
  expect(response.status()).toBe(404);
  expect(response.headers()["content-type"]).toContain("application/json");
});
