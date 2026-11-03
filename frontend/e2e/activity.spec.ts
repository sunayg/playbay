import { expect, test } from "@playwright/test";

const HASH = "a".repeat(40);
const activity = {
  streams: [
    { id: "s1", hash: HASH, filename: "Nocturne.2026.S01E02.1080p.HEVC.x265-MeGusta.mkv", encoder: "h264_vaapi", hdr: "pq", levels: [1080, 720, 480], duration: 2700, position_seconds: 600, encoded_seconds: 1500, paused: true, speed: null, restarts: 2, idle_seconds: 4, failed: false },
  ],
  downloads: [
    { hash: HASH, filename: "Next.Episode.2160p.mkv", status: "downloading", progress: 0.37, peers: 41, download_rate: 4_200_000, size: null, age_seconds: null },
    { hash: HASH, filename: "Nocturne.2026.S01E02.1080p.HEVC.x265-MeGusta.mkv", status: "ready", progress: 1, peers: null, download_rate: null, size: 2_900_000_000, age_seconds: 7200 },
  ],
  disk: { total: 500_000_000_000, free: 120_000_000_000 },
};

test("the activity page shows streams, downloads and kept files, and ends a stream", async ({ page }) => {
  const posts: string[] = [];
  await page.route("**/api/activity", (route) => route.fulfill({ json: activity }));
  await page.route("**/api/activity/**", (route) => {
    posts.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto("/activity");
  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();
  await expect(page.locator("[data-kind=stream]")).toContainText("Waiting for you: encoded far enough ahead");
  await expect(page.locator("[data-kind=stream]")).toContainText("1080p 720p 480p · h264_vaapi · HDR to SDR");
  await expect(page.locator("[data-kind=download]")).toContainText("37% · 4.2 MB/s · 41 peers");
  await expect(page.locator("[data-kind=kept]")).toContainText("2.9 GB");
  await expect(page.getByText("120 GB free of 500 GB")).toBeVisible();
  await page.screenshot({ path: "test-results/activity-desktop.png", fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "test-results/activity-phone.png", fullPage: true });
  // Nothing scrolls sideways on a phone.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await page.locator("[data-kind=stream] button").click();
  await expect.poll(() => posts).toContain("POST /api/activity/streams/s1/end");
});

test("the home screen has an Activity shortcut", async ({ page }) => {
  await page.route("**/api/activity", (route) => route.fulfill({ json: { streams: [], downloads: [], disk: {} } }));
  await page.goto("/");
  await page.getByRole("button", { name: "Activity" }).click();
  await expect(page).toHaveURL(/\/activity$/);
});
