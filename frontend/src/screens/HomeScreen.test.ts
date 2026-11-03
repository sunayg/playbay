import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createMemoryHistory } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App.vue";
import { uninstallFocus } from "../lib/focus";
import * as libraryModule from "../lib/library";
import { library, refreshLibrary } from "../lib/library";
import { encodeMagnetParam } from "../lib/magnet";
import { createAppRouter } from "../router";

vi.mock("../components/SettingsPage.vue", () => ({ default: { template: "<div class='settings-stub'>x</div>" } }));

const series = [
  { tmdb_id: 11, media_type: "tv", title: "MobLand", rank: 1, poster_url: "http://img/p11.jpg", backdrop_url: "http://img/b11.jpg", next_air_date: "2099-10-30" },
  { tmdb_id: 12, media_type: "tv", title: "Severance", rank: 2, poster_url: "http://img/p12.jpg", backdrop_url: "http://img/b12.jpg" },
];
const movies = [{ tmdb_id: 21, media_type: "movie", title: "Dune", rank: 1, poster_url: "http://img/p21.jpg", backdrop_url: "http://img/b21.jpg" }];
const keep = [
  { tmdb_id: 31, media_type: "tv", title: "Andor", backdrop_url: "http://img/b31.jpg", subtitle: "S01E02", minutes_left: 25, magnet: "magnet:?xt=urn:btih:ABC", filename: "Andor.S01E02.mkv" },
  { tmdb_id: 32, media_type: "movie", title: "Heat", backdrop_url: "http://img/b32.jpg", year: 1995 },
];
const recent = [{ tmdb_id: 41, media_type: "tv", title: "Fargo", backdrop_url: "http://img/b41.jpg", next_air_date: "2099-01-05", watched_episodes: [{ season: 2, episode: 3, at: 1 }] }];

let mounted: VueWrapper | null = null;

async function visit(path = "/") {
  const router = createAppRouter(createMemoryHistory("/"));
  await router.push(path);
  await router.isReady();
  const wrapper = mount(App, { attachTo: document.body, global: { plugins: [router] } });
  mounted = wrapper;
  await flushPromises();
  return { router, wrapper };
}

function mockData(over: { series?: unknown[]; movies?: unknown[]; keep?: unknown[]; recent?: unknown[] } = {}) {
  vi.spyOn(libraryModule, "fetchHome").mockResolvedValue({ series: over.series ?? series, movies: over.movies ?? movies } as never);
  vi.spyOn(libraryModule, "fetchWatchRows").mockResolvedValue({ keep_watching: over.keep ?? keep, recent: over.recent ?? recent } as never);
}

beforeEach(() => {
  localStorage.clear();
  refreshLibrary();
  vi.spyOn(libraryModule, "syncLibraryFromLocalHistory").mockResolvedValue();
  vi.spyOn(libraryModule, "reportLibraryEvents").mockResolvedValue(true);
});

afterEach(() => {
  mounted?.unmount();
  mounted = null;
  uninstallFocus();
  document.documentElement.classList.remove("tv-ui");
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("HomeScreen rows", () => {
  it("renders the keep watching, recent and top rows from API data", async () => {
    mockData();
    const { wrapper } = await visit();
    const headings = wrapper.findAll(".home-row-heading").map((h) => h.text());
    expect(headings).toEqual(["Keep watching", "Recently watched", "Trending series this week", "Trending movies this week"]);
    expect(wrapper.findAll(".billboard-card")).toHaveLength(3);
    expect(wrapper.findAll(".poster-card")).toHaveLength(3);
    expect(wrapper.findAll(".rank-svg")).toHaveLength(3);
    const first = wrapper.findAll(".billboard-card")[0];
    expect(first.text()).toContain("Andor");
    expect(first.text()).toContain("S01E02");
    expect(first.text()).toContain("25 min left");
    expect(first.find("img").attributes("src")).toBe("http://img/b31.jpg");
    // Movies fall back to the year, shows to the latest watched episode.
    expect(wrapper.findAll(".billboard-card")[1].text()).toContain("1995");
    const fargo = wrapper.findAll(".billboard-card")[2];
    expect(fargo.text()).toContain("S02E03");
    expect(fargo.text()).toContain("Next");
    expect(wrapper.find(".home-empty").exists()).toBe(false);
    expect(wrapper.find(".spinner").exists()).toBe(false);
  });

  it("shows a spinner until the rows have loaded, then the empty state", async () => {
    let release: (v: never) => void = () => {};
    vi.spyOn(libraryModule, "fetchHome").mockReturnValue(new Promise((resolve) => (release = resolve as never)));
    vi.spyOn(libraryModule, "fetchWatchRows").mockResolvedValue({ keep_watching: [], recent: [] });
    const { wrapper } = await visit();
    expect(wrapper.find(".spinner").exists()).toBe(true);
    expect(wrapper.find(".home-empty").exists()).toBe(false);
    release({ series: [], movies: [] } as never);
    await flushPromises();
    expect(wrapper.find(".spinner").exists()).toBe(false);
    expect(wrapper.find(".home-empty").text()).toBe("Search for a movie or show to get started.");
    expect(wrapper.findAll(".home-shortcut")).toHaveLength(5);
  });

  it("still shows local rows and the empty state when /api/home fails", async () => {
    vi.spyOn(libraryModule, "fetchHome").mockRejectedValue(new Error("down"));
    vi.spyOn(libraryModule, "fetchWatchRows").mockResolvedValue({ keep_watching: keep, recent: [] } as never);
    const { wrapper } = await visit();
    expect(wrapper.findAll(".home-row-heading").map((h) => h.text())).toEqual(["Keep watching"]);
  });

  it("looks again once when artwork is missing", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    mockData({ series: [{ tmdb_id: 1, media_type: "tv", title: "NoArt", rank: 1 }], movies: [], keep: [], recent: [] });
    await visit();
    const rows = vi.mocked(libraryModule.fetchWatchRows);
    expect(rows).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2600);
    expect(rows).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(5000);
    expect(rows).toHaveBeenCalledTimes(2);
  });
});

describe("HomeScreen navigation", () => {
  it("submits a search to /search/:term and remembers it", async () => {
    mockData({ series: [], movies: [], keep: [], recent: [] });
    const { wrapper, router } = await visit();
    await wrapper.find("input.form-control").setValue("the matrix & co");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(router.currentRoute.value.path).toBe("/search/the%20matrix%20%26%20co");
    expect(router.currentRoute.value.params.searchterm).toBe("the matrix & co");
    expect(library.searchHistory).toEqual(["the matrix & co"]);
  });

  it("sends a pasted magnet link to the file list instead", async () => {
    mockData({ series: [], movies: [], keep: [], recent: [] });
    const { wrapper, router } = await visit();
    const magnet = "magnet:?xt=urn:btih:ABCDEF&dn=Some+Movie";
    await wrapper.find("input.form-control").setValue(magnet);
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(router.currentRoute.value.name).toBe("filelist");
    expect(router.currentRoute.value.fullPath).toContain(encodeMagnetParam(magnet));
    expect(library.searchHistory).toEqual([]);
  });

  it("a top title opens a search pinned to its TMDB id", async () => {
    mockData();
    const { wrapper, router } = await visit();
    await wrapper.findAll(".poster-card")[0].trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.path).toBe("/search/MobLand");
    expect(router.currentRoute.value.query).toEqual({ media: "tv", tmdb: "11" });
    expect(library.searchHistory).toEqual(["MobLand"]);
  });

  it("keep watching resumes the file; without one it searches", async () => {
    mockData();
    const { wrapper, router } = await visit();
    const cards = wrapper.findAll(".billboard-card");
    await cards[0].trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.name).toBe("download");
    expect(router.currentRoute.value.params.filename).toBe("Andor.S01E02.mkv");
    await router.push("/");
    await flushPromises();
    await wrapper.findAll(".billboard-card")[1].trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.path).toBe("/search/Heat");
    expect(router.currentRoute.value.query).toEqual({ media: "movie", tmdb: "32" });
  });

  it("history chip click searches that term; clear removes the chips", async () => {
    mockData({ series: [], movies: [], keep: [], recent: [] });
    localStorage.setItem("searchHistory", JSON.stringify(["dune", "alien"]));
    const { wrapper, router } = await visit();
    const chips = wrapper.findAll(".search-chip");
    expect(chips.map((c) => c.text())).toEqual(["dune", "alien"]);
    await chips[1].trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.path).toBe("/search/alien");
    expect(library.searchHistory[0]).toBe("alien");

    await router.push("/");
    await flushPromises();
    await wrapper.find(".search-history-clear").trigger("click");
    expect(wrapper.findAll(".search-chip")).toHaveLength(0);
    expect(wrapper.find(".search-history-clear").exists()).toBe(false);
    expect(localStorage.getItem("searchHistory")).toBeNull();
    // Nothing left to show: the empty state appears.
    expect(wrapper.find(".home-empty").exists()).toBe(true);
  });

  it.each([
    ["History", "/search/[history]"],
    ["Latest releases", "/search/"],
    ["Favorites", "/search/[favorites]"],
    ["Activity", "/activity"],
    ["Settings", "/settings"],
  ])("the %s shortcut goes to %s", async (label, path) => {
    mockData({ series: [], movies: [], keep: [], recent: [] });
    const { wrapper, router } = await visit();
    const button = wrapper.findAll(".home-shortcut").find((b) => b.text() === label)!;
    await button.trigger("click");
    await flushPromises();
    expect(decodeURIComponent(router.currentRoute.value.path)).toBe(path);
  });

  it("an unknown path shows the home screen", async () => {
    mockData();
    const { wrapper } = await visit("/whatever/else");
    expect(wrapper.find('[data-screen="search-screen"]').exists()).toBe(true);
  });

  it("registers the magnet handler on /registerhandler", async () => {
    mockData();
    const register = vi.fn();
    Object.defineProperty(navigator, "registerProtocolHandler", { value: register, configurable: true });
    await visit("/registerhandler");
    expect(register).toHaveBeenCalledWith("magnet", expect.stringMatching(/\/magnet\/%s$/));
  });
});

// jsdom has no layout, so give elements a box.
function box(el: Element, left: number, top: number, w: number, h: number) {
  el.getBoundingClientRect = () =>
    ({ left, top, right: left + w, bottom: top + h, width: w, height: h, x: left, y: top, toJSON() {} }) as DOMRect;
}
function press(key: string) {
  document.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
}

describe("HomeScreen remote and keyboard focus", () => {
  it("walks the home targets with the arrow keys and Enter activates", async () => {
    mockData();
    localStorage.setItem("searchHistory", JSON.stringify(["dune"]));
    const { wrapper, router } = await visit();
    // Stack the sections vertically with a fake layout.
    const input = wrapper.find("input.form-control").element;
    box(input, 20, 10, 300, 40);
    box(wrapper.find(".home-rows").element, 0, 60, 1000, 800);
    const sections = wrapper.findAll(".home-row");
    sections.forEach((s, i) => box(s.element, 0, 100 + i * 200, 1000, 180));
    const place = (el: Element, x: number, rowTop: number) => box(el, x, rowTop + 20, 100, 100);
    wrapper.findAll(".billboard-card").forEach((c, i) => place(c.element, 40 + (i % 2) * 120, i < 2 ? 100 : 300));
    wrapper.findAll(".poster-card").forEach((c, i) => place(c.element, 40 + i * 120, i < 2 ? 500 : 700));
    box(wrapper.find(".search-chip").element, 40, 920, 80, 36);
    box(wrapper.find(".search-history-clear").element, 40, 980, 120, 30);
    wrapper.findAll(".home-shortcut").forEach((b, i) => box(b.element, 400 + i * 120, 10, 100, 40));

    const active = () => document.activeElement as HTMLElement;
    // The search box is focused on load.
    expect(active()).toBe(input);

    press("ArrowDown"); // from the search box to the first row
    expect(active().textContent).toContain("Andor");
    press("ArrowRight");
    expect(active().textContent).toContain("Heat");
    press("ArrowLeft");
    expect(active().textContent).toContain("Andor");
    press("ArrowDown");
    expect(active().textContent).toContain("Fargo");
    press("ArrowDown");
    expect(active().getAttribute("aria-label")).toBe("MobLand");
    press("ArrowRight");
    expect(active().getAttribute("aria-label")).toBe("Severance");
    press("ArrowDown");
    expect(active().getAttribute("aria-label")).toBe("Dune");
    press("ArrowDown");
    expect(active().className).toContain("search-chip");
    press("ArrowDown");
    expect(active().className).toContain("search-history-clear");
    press("ArrowDown"); // nothing below the last row: stays put
    expect(active().className).toContain("search-history-clear");

    // Enter on a focused card clicks it.
    press("ArrowUp"); // chip
    press("ArrowUp"); // movies row
    press("ArrowUp"); // series row
    expect(active().getAttribute("aria-label")).toBe("Severance");
    press("Enter");
    await flushPromises();
    expect(router.currentRoute.value.path).toBe("/search/Severance");
  });

  it("left and right stay in the search box for caret movement", async () => {
    mockData();
    const { wrapper } = await visit();
    const input = wrapper.find("input.form-control").element as HTMLInputElement;
    box(input, 20, 10, 300, 40);
    wrapper.findAll(".home-shortcut").forEach((b, i) => box(b.element, 400 + i * 120, 10, 100, 40));
    expect(document.activeElement).toBe(input);
    input.value = "dune";
    input.setSelectionRange(1, 1);
    press("ArrowRight");
    expect(document.activeElement).toBe(input);
  });

  it("right at the end of the search text moves to the header buttons", async () => {
    mockData();
    const { wrapper } = await visit();
    const input = wrapper.find("input.form-control").element as HTMLInputElement;
    box(input, 20, 10, 300, 40);
    wrapper.findAll(".home-shortcut").forEach((b, i) => box(b.element, 400 + i * 120, 10, 100, 40));
    input.value = "dune";
    input.setSelectionRange(4, 4);
    press("ArrowRight");
    expect((document.activeElement as HTMLElement).textContent).toContain("History");
    press("ArrowRight");
    press("ArrowRight");
    expect((document.activeElement as HTMLElement).textContent).toContain("Favorites");
  });

  it("does not grab the search box on the TV UI", async () => {
    document.documentElement.classList.add("tv-ui");
    mockData();
    const { wrapper } = await visit();
    expect(document.activeElement).not.toBe(wrapper.find("input.form-control").element);
  });
});
