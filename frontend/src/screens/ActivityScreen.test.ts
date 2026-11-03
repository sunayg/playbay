import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createMemoryHistory } from "vue-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App.vue";
import { createAppRouter } from "../router";

const HASH = "a".repeat(40);
const activity = {
  streams: [
    { id: "s1", hash: HASH, filename: "Show.S01E01.mkv", encoder: "h264_vaapi", hdr: "sdr", levels: [1080, 720], duration: 3000, position_seconds: 600, encoded_seconds: 900, paused: false, speed: 1.8, restarts: 1, idle_seconds: 2, failed: false },
  ],
  downloads: [
    { hash: HASH, filename: "Next.mkv", status: "downloading", progress: 0.5, peers: 8, download_rate: 1_000_000, size: null, age_seconds: null },
    { hash: HASH, filename: "Show.S01E01.mkv", status: "ready", progress: 1, peers: null, download_rate: null, size: 2_000_000_000, age_seconds: 90000 },
  ],
  disk: { total: 500_000_000_000, free: 120_000_000_000 },
};

let wrapper: VueWrapper | null = null;
const calls: { url: string; method: string }[] = [];

function stubFetch(state = activity) {
  calls.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), method: init?.method ?? "GET" });
      return { ok: true, status: 200, headers: new Headers(), json: async () => (String(url) === "/api/activity" ? state : { ok: true }) } as Response;
    }),
  );
}

async function visit() {
  const router = createAppRouter(createMemoryHistory("/"));
  await router.push("/activity");
  await router.isReady();
  wrapper = mount(App, { attachTo: document.body, global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("ActivityScreen", () => {
  it("lists the streams, the downloads, the kept files and the free disk space", async () => {
    stubFetch();
    const w = await visit();
    const text = w.text();
    expect(text).toContain("Show.S01E01.mkv");
    expect(text).toContain("1080p 720p · h264_vaapi");
    expect(text).toContain("Encoding at 1.8× real time");
    expect(text).toContain("restarted 1×");
    expect(text).toContain("At 10:00 of 50:00, encoded to 15:00");
    expect(w.find("[data-kind=download]").text()).toContain("50% · 1.0 MB/s · 8 peers");
    expect(w.find("[data-kind=kept]").text()).toContain("2.0 GB · kept since 1 day ago");
    expect(text).toContain("120 GB free of 500 GB");
  });

  it("says so when nothing is happening", async () => {
    stubFetch({ streams: [], downloads: [], disk: {} } as never);
    const text = (await visit()).text();
    expect(text).toContain("Nothing is being watched.");
    expect(text).toContain("No downloads in progress.");
    expect(text).toContain("No files are kept.");
  });

  it("ends a stream", async () => {
    stubFetch();
    const w = await visit();
    await w.find("[data-kind=stream] button").trigger("click");
    await flushPromises();
    expect(calls).toContainEqual({ url: "/api/activity/streams/s1/end", method: "POST" });
  });

  it("cancels a download at once but asks again before deleting a kept file", async () => {
    stubFetch();
    const w = await visit();
    await w.find("[data-kind=download] button").trigger("click");
    await flushPromises();
    expect(calls).toContainEqual({ url: `/api/activity/downloads/${HASH}/Next.mkv/remove`, method: "POST" });

    calls.length = 0;
    const del = w.find("[data-kind=kept] button");
    await del.trigger("click");
    expect(calls.filter((c) => c.method === "POST")).toEqual([]);
    expect(del.text()).toBe("Delete for good?");
    await del.trigger("click");
    await flushPromises();
    expect(calls).toContainEqual({ url: `/api/activity/downloads/${HASH}/Show.S01E01.mkv/remove`, method: "POST" });
  });

  it("refreshes by itself", async () => {
    vi.useFakeTimers();
    stubFetch();
    await visit();
    const before = calls.filter((c) => c.url === "/api/activity").length;
    await vi.advanceTimersByTimeAsync(3100);
    expect(calls.filter((c) => c.url === "/api/activity").length).toBe(before + 1);
  });
});
