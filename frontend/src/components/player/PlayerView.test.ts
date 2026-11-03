import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createMemoryHistory } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installFocus, uninstallFocus } from "../../lib/focus";
import { getVideoPosition, readWatchLog, resetProgressThrottle, saveVideoPosition } from "../../lib/library";
import { magnetPath } from "../../lib/magnet";
import { storageGet, storageSet } from "../../lib/storage";
import { createAppRouter } from "../../router";
import PlayerView from "./PlayerView.vue";
import { FakeHls, FakeTrack, installFakeMedia, mediaState, restoreFakeMedia } from "./testing";

vi.mock("hls.js", () => ({ default: FakeHls }));

const MAGNET = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=Show";
const HASH = "abcdef0123456789abcdef0123456789abcdef01";
const FILE = "Show.S01E01.1080p.mp4";
const MP4 = `/play/${HASH}/${encodeURIComponent(FILE)}`;
const HLS = `/play/${HASH}/hls%2Fmaster.m3u8?subs=0`;

let fetched: { url: string; init?: RequestInit }[] = [];
let responses: Record<string, unknown> = {};
let wrapper: VueWrapper | null = null;

function stubFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      fetched.push({ url, init });
      const key = Object.keys(responses)
        .sort((a, b) => b.length - a.length)
        .find((prefix) => url.startsWith(prefix));
      if (!key) return new Response("{}", { status: 500 });
      return new Response(JSON.stringify(responses[key]), { status: 200 });
    }),
  );
}

const libraryPosts = () => fetched.filter((f) => f.url.startsWith("/api/library/observe/"));

async function mountPlayer(props: Record<string, unknown> = {}) {
  const router = createAppRouter(createMemoryHistory("/"));
  await router.push("/magnet/x/y");
  await router.isReady();
  wrapper = mount(PlayerView, {
    attachTo: document.body,
    props: {
      url: MP4,
      supported: true,
      subtitles: [],
      magnet: MAGNET,
      filename: FILE,
      downloadProgress: null,
      downloadStatus: null,
      ...props,
    },
    global: { plugins: [router] },
  });
  await flushPromises();
  const video = (wrapper.find("video").exists() ? wrapper.find("video").element : document.createElement("video")) as HTMLVideoElement;
  return { router, video, state: mediaState(video) };
}

function press(key: string, init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  document.body.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  resetProgressThrottle();
  fetched = [];
  responses = { "/api/next_file/": { next_filename: null, next_magnet: null }, "/api/library/observe/": {} };
  stubFetch();
  installFakeMedia();
  installFocus();
  FakeHls.instances = [];
  FakeHls.supported = true;
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  uninstallFocus();
  restoreFakeMedia();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("mp4 playback", () => {
  it("plays the file and resumes from the saved position", async () => {
    saveVideoPosition(MAGNET, FILE, 321, 3000);
    const { video, state } = await mountPlayer();
    expect(video.getAttribute("src")).toBe(window.location.origin + MP4);
    expect(state.playCalls).toBeGreaterThan(0);
    video.dispatchEvent(new Event("loadedmetadata"));
    expect(state.currentTime).toBe(321);
  });

  it("starts at the beginning when nothing was saved", async () => {
    const { video, state } = await mountPlayer();
    video.dispatchEvent(new Event("loadedmetadata"));
    expect(state.currentTime).toBe(0);
  });

  it("shows a message instead of a video for an unsupported file", async () => {
    await mountPlayer({ supported: false });
    expect(wrapper!.find("video").exists()).toBe(false);
    expect(wrapper!.text()).toContain("can't be played");
  });

  it("emits back from the back button", async () => {
    await mountPlayer();
    await wrapper!.find('[aria-label="Back"]').trigger("click");
    expect(wrapper!.emitted("back")).toHaveLength(1);
  });

  it("shows the download progress badge while the file is still downloading", async () => {
    await mountPlayer({ downloadProgress: 0.42, downloadStatus: "downloading" });
    expect(wrapper!.find(".player-badge").text()).toContain("42%");
    await wrapper!.setProps({ downloadProgress: null });
    expect(wrapper!.find(".player-badge").exists()).toBe(false);
  });
});

describe("watched progress", () => {
  it("saves the position every 5 seconds and reports it at most every 30", async () => {
    vi.useFakeTimers();
    const { video, state } = await mountPlayer();
    state.paused = false;
    state.duration = 3000;
    state.currentTime = 200;
    video.dispatchEvent(new Event("timeupdate"));
    await vi.advanceTimersByTimeAsync(5000);
    expect(getVideoPosition(MAGNET, FILE)).toBe(200);
    expect(libraryPosts()).toHaveLength(1);
    state.currentTime = 210;
    await vi.advanceTimersByTimeAsync(5000);
    await vi.advanceTimersByTimeAsync(5000);
    expect(getVideoPosition(MAGNET, FILE)).toBe(210);
    expect(libraryPosts()).toHaveLength(1); // throttled
    state.currentTime = 260;
    await vi.advanceTimersByTimeAsync(20000);
    expect(libraryPosts().length).toBeGreaterThanOrEqual(2);
    const body = JSON.parse(String(libraryPosts()[0].init?.body));
    expect(body.events[0]).toMatchObject({ event: "progress", filename: FILE, position: 200, duration: 3000 });
  });

  it("saves and reports at once on pause", async () => {
    const { video, state } = await mountPlayer();
    state.duration = 3000;
    state.currentTime = 500;
    video.dispatchEvent(new Event("pause"));
    expect(getVideoPosition(MAGNET, FILE)).toBe(500);
    expect(libraryPosts()).toHaveLength(1);
  });

  it("marks the file watched near the end and stops saving a position", async () => {
    saveVideoPosition(MAGNET, FILE, 100, 3000);
    const { video, state } = await mountPlayer();
    state.duration = 3000;
    state.currentTime = 2990;
    video.dispatchEvent(new Event("timeupdate"));
    expect(getVideoPosition(MAGNET, FILE)).toBe(0);
    expect(readWatchLog().some((e) => e.event === "watched" && e.filename === FILE)).toBe(true);
    video.dispatchEvent(new Event("pause"));
    expect(getVideoPosition(MAGNET, FILE)).toBe(0);
  });
});

describe("next file", () => {
  beforeEach(() => {
    responses["/api/next_file/"] = { next_filename: "Show.S01E02.1080p.mp4", next_magnet: null };
    responses["/api/magnet_download/"] = {};
  });

  it("looks up the next file, offers it and prefetches it on play", async () => {
    const { video } = await mountPlayer();
    expect(fetched[0].url).toBe(`/api/next_file/${HASH}/${encodeURIComponent(FILE)}`);
    await wrapper!.find("video").trigger("click"); // reveal nothing: just ensure no crash
    video.dispatchEvent(new Event("play"));
    await flushPromises();
    const prefetch = fetched.find((f) => f.url === "/api/magnet_download/");
    expect(prefetch).toBeTruthy();
    expect(Object.fromEntries((prefetch!.init!.body as URLSearchParams).entries())).toEqual({
      magnet_link: MAGNET,
      filename: "Show.S01E02.1080p.mp4",
    });
    video.dispatchEvent(new Event("play"));
    await flushPromises();
    expect(fetched.filter((f) => f.url === "/api/magnet_download/")).toHaveLength(1);
    expect(wrapper!.find('[aria-label="Next episode"]').exists()).toBe(true);
  });

  it("goes to the next file when playback ends", async () => {
    const { video, router } = await mountPlayer();
    video.dispatchEvent(new Event("ended"));
    await flushPromises();
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe(magnetPath(MAGNET, "Show.S01E02.1080p.mp4"));
  });

  it("goes to a next episode that lives in another torrent", async () => {
    responses["/api/next_file/"] = { next_filename: "Show.S01E02.mkv", next_magnet: "magnet:?xt=urn:btih:BBBB" };
    const { video, router } = await mountPlayer();
    video.dispatchEvent(new Event("ended"));
    await flushPromises();
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe(magnetPath("magnet:?xt=urn:btih:BBBB", "Show.S01E02.mkv"));
  });

  it("stays when there is no next file (AUTO_PLAY_NEXT_FILE off or last episode)", async () => {
    responses["/api/next_file/"] = { next_filename: null, next_magnet: null };
    const { video, router } = await mountPlayer();
    const before = router.currentRoute.value.fullPath;
    video.dispatchEvent(new Event("ended"));
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe(before);
    expect(wrapper!.find('[aria-label="Next episode"]').exists()).toBe(false);
  });

  it("the next button and the media-next key both open it", async () => {
    const { router } = await mountPlayer();
    await wrapper!.find('[aria-label="Next episode"]').trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe(magnetPath(MAGNET, "Show.S01E02.1080p.mp4"));
    await router.push("/magnet/x/y");
    press("MediaTrackNext");
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe(magnetPath(MAGNET, "Show.S01E02.1080p.mp4"));
  });
});

describe("keyboard, remote and media keys", () => {
  it("space, K and Enter toggle play and pause", async () => {
    const { state } = await mountPlayer();
    state.paused = true; // autoplay was refused
    const plays = state.playCalls;
    expect(press(" ").defaultPrevented).toBe(true);
    expect(state.playCalls).toBe(plays + 1);
    expect(state.paused).toBe(false);
    press("k");
    expect(state.paused).toBe(true);
    press("Enter");
    expect(state.paused).toBe(false);
  });

  it("arrow keys seek +60 and -15 and are not used for focus movement", async () => {
    const { state } = await mountPlayer();
    state.duration = 1000;
    state.currentTime = 100;
    const right = press("ArrowRight");
    expect(right.defaultPrevented).toBe(true);
    expect(state.currentTime).toBe(160);
    press("ArrowLeft");
    expect(state.currentTime).toBe(145);
    state.currentTime = 990;
    press("ArrowRight");
    expect(state.currentTime).toBe(1000);
    state.currentTime = 5;
    press("ArrowLeft");
    expect(state.currentTime).toBe(0);
  });

  it("D-pad keycodes seek too", async () => {
    const { state } = await mountPlayer();
    state.duration = 1000;
    state.currentTime = 100;
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Unidentified", keyCode: 22, bubbles: true, cancelable: true }));
    expect(state.currentTime).toBe(160);
  });

  it("M mutes and unmutes", async () => {
    const { state } = await mountPlayer();
    press("m");
    expect(state.muted).toBe(true);
    press("m");
    expect(state.muted).toBe(false);
  });

  it("media keys: play/pause, play, pause and fast-forward", async () => {
    const { state } = await mountPlayer();
    state.duration = 1000;
    state.paused = true;
    press("MediaPlayPause");
    expect(state.paused).toBe(false);
    press("MediaPause");
    expect(state.paused).toBe(true);
    press("MediaPlay");
    expect(state.paused).toBe(false);
    state.currentTime = 10;
    press("MediaFastForward");
    expect(state.currentTime).toBe(40);
    // Legacy keycodes sent by some remotes.
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Unidentified", keyCode: 179, bubbles: true, cancelable: true }));
    expect(state.paused).toBe(true);
  });

  it("media keys work while a control has focus", async () => {
    const { state } = await mountPlayer();
    state.paused = true;
    (wrapper!.find('[aria-label="Mute"]').element as HTMLElement).focus();
    press("MediaPlayPause");
    expect(state.paused).toBe(false);
  });

  it("plain keys leave a focused control alone", async () => {
    const { state } = await mountPlayer();
    state.currentTime = 100;
    state.duration = 1000;
    (wrapper!.find('[aria-label="Mute"]').element as HTMLElement).focus();
    press("ArrowRight");
    expect(state.currentTime).toBe(100);
  });

  it("ignores keys with a modifier", async () => {
    const { state } = await mountPlayer();
    press("f", { ctrlKey: true });
    press("m", { metaKey: true });
    expect(state.muted).toBe(false);
  });

  it("any key shows the controls again after they auto-hid", async () => {
    vi.useFakeTimers();
    await mountPlayer();
    expect(wrapper!.find(".player-controls").isVisible()).toBe(true);
    await vi.advanceTimersByTimeAsync(3000);
    expect(wrapper!.find(".player-controls").isVisible()).toBe(false);
    expect(wrapper!.classes()).toContain("controls-hidden");
    press("ArrowDown");
    await flushPromises();
    expect(wrapper!.find(".player-controls").isVisible()).toBe(true);
  });

  it("controls stay while a menu is open", async () => {
    vi.useFakeTimers();
    await mountPlayer();
    await wrapper!.find('[aria-label="Quality, audio and subtitles"]').trigger("click");
    await vi.advanceTimersByTimeAsync(10000);
    expect(wrapper!.find(".player-controls").isVisible()).toBe(true);
  });

  it("Escape closes an open menu and returns focus to its button; otherwise it does nothing", async () => {
    await mountPlayer();
    const opener = wrapper!.find('[aria-label="Quality, audio and subtitles"]');
    expect(press("Escape").defaultPrevented).toBe(false);
    await opener.trigger("click");
    expect(wrapper!.find(".player-tracks").exists()).toBe(true);
    expect(press("Escape").defaultPrevented).toBe(true);
    await flushPromises();
    expect(wrapper!.find(".player-tracks").exists()).toBe(false);
    expect(document.activeElement).toBe(opener.element);
  });

  it("the buttons seek 10 seconds and mute", async () => {
    const { state } = await mountPlayer();
    state.duration = 600;
    state.currentTime = 100;
    await wrapper!.find('[aria-label="Forward 10 seconds"]').trigger("click");
    expect(state.currentTime).toBe(110);
    await wrapper!.find('[aria-label="Back 10 seconds"]').trigger("click");
    expect(state.currentTime).toBe(100);
    await wrapper!.find('[aria-label="Mute"]').trigger("click");
    expect(state.muted).toBe(true);
  });

  it("the seek bar jumps to the matching time and shows elapsed and total length", async () => {
    const { video, state } = await mountPlayer();
    state.duration = 600;
    video.dispatchEvent(new Event("durationchange"));
    const scrub = wrapper!.find(".player-scrub");
    (scrub.element as HTMLInputElement).value = "500";
    await scrub.trigger("input");
    expect(state.currentTime).toBe(300);
    await flushPromises();
    expect(wrapper!.find(".player-elapsed").text()).toBe("5:00");
    expect(wrapper!.find(".player-total").text()).toBe("10:00");
    expect(wrapper!.find(".player-total").attributes("title")).toBe("Time remaining 5:00");
  });
});

describe("playing from an encode session", () => {
  it("looks up the next file under the torrent's own hash, not the one in the stream URL", async () => {
    await mountPlayer({ url: "/api/hls/0123456789abcdef0123456789abcdef/master.m3u8" });
    const lookups = fetched.map((f) => f.url).filter((u) => u.startsWith("/api/next_file/"));
    expect(lookups.length).toBeGreaterThan(0);
    expect(lookups.every((u) => u.startsWith(`/api/next_file/${HASH}/`))).toBe(true);
  });
});

describe("seek bar feedback", () => {
  function fakeBuffered(video: HTMLVideoElement, ranges: [number, number][]) {
    Object.defineProperty(video, "buffered", {
      configurable: true,
      value: { length: ranges.length, start: (i: number) => ranges[i][0], end: (i: number) => ranges[i][1] },
    });
  }

  it("shows how much the browser has loaded, as a share of the whole length", async () => {
    const { video, state } = await mountPlayer();
    state.duration = 600;
    state.currentTime = 100;
    fakeBuffered(video, [[0, 150]]);
    video.dispatchEvent(new Event("durationchange"));
    video.dispatchEvent(new Event("progress"));
    await flushPromises();
    expect(wrapper!.find(".player-buffered").attributes("style")).toContain("width: 25%");
  });

  it("only counts the loaded range that holds the playhead", async () => {
    const { video, state } = await mountPlayer();
    state.duration = 1000;
    state.currentTime = 500;
    fakeBuffered(video, [[0, 100], [490, 600]]); // an earlier part, and the part being played
    video.dispatchEvent(new Event("durationchange"));
    video.dispatchEvent(new Event("progress"));
    await flushPromises();
    expect(wrapper!.find(".player-buffered").attributes("style")).toContain("width: 60%");
  });

  it("marks how far the download has got while the file is still downloading", async () => {
    const { video, state } = await mountPlayer({ downloadProgress: 0.42, downloadStatus: "downloading" });
    state.duration = 600;
    video.dispatchEvent(new Event("durationchange"));
    await flushPromises();
    expect(wrapper!.find(".player-limit").attributes("style")).toContain("left: 42%");
    expect(wrapper!.find(".player-downloaded").attributes("style")).toContain("width: 42%");
    await wrapper!.setProps({ downloadProgress: null });
    expect(wrapper!.find(".player-limit").exists()).toBe(false);
    expect(wrapper!.find(".player-downloaded").exists()).toBe(false);
  });
});

describe("captions", () => {
  function addTracks(state: ReturnType<typeof mediaState>) {
    const english = new FakeTrack("en", "subtitles", "English");
    const dutch = new FakeTrack("nl", "subtitles", "Dutch");
    state.textTracks.add(english);
    state.textTracks.add(dutch);
    return { english, dutch };
  }

  it("the caption button cycles through the tracks and back to off", async () => {
    const { state } = await mountPlayer();
    expect(wrapper!.find(".caption-btn").exists()).toBe(false);
    const { english, dutch } = addTracks(state);
    await flushPromises();
    const button = wrapper!.find(".caption-btn");
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    expect([english.mode, dutch.mode]).toEqual(["showing", "hidden"]);
    state.textTracks.dispatchEvent(new Event("change"));
    await flushPromises();
    expect(wrapper!.find(".caption-btn").text()).toBe("en");
    await wrapper!.find(".caption-btn").trigger("click");
    expect([english.mode, dutch.mode]).toEqual(["hidden", "showing"]);
    await wrapper!.find(".caption-btn").trigger("click");
    expect([english.mode, dutch.mode]).toEqual(["hidden", "hidden"]);
    press("c");
    expect(english.mode).toBe("showing");
  });

  it("the tracks menu picks a subtitle, remembers the language and can switch it off", async () => {
    const { state } = await mountPlayer();
    const { english, dutch } = addTracks(state);
    await wrapper!.find('[aria-label="Quality, audio and subtitles"]').trigger("click");
    const labels = wrapper!.findAll(".player-tracks button").map((b) => b.text());
    expect(labels).toEqual(["Original", "Off", "English", "Dutch"]);
    await wrapper!.findAll(".player-tracks button")[3].trigger("click");
    expect(dutch.mode).toBe("showing");
    expect(english.mode).toBe("disabled");
    expect(storageGet("captionLanguage")).toBe("nl");
    await wrapper!.findAll(".player-tracks button")[1].trigger("click");
    expect(dutch.mode).toBe("disabled");
    expect(storageGet("captionLanguage")).toBeNull();
  });

  it("applies the remembered language when tracks appear, normalising region tags", async () => {
    storageSet("captionLanguage", "nl-BE");
    const { state } = await mountPlayer();
    const { english, dutch } = addTracks(state);
    expect(english.mode).toBe("disabled");
    expect(dutch.mode).toBe("showing");
  });

  it("keeps only the newest of several showing tracks", async () => {
    const { state } = await mountPlayer();
    const { english, dutch } = addTracks(state);
    english.mode = "showing";
    dutch.mode = "showing";
    state.textTracks.dispatchEvent(new Event("change"));
    expect([english.mode, dutch.mode]).toEqual(["hidden", "showing"]);
    expect(storageGet("captionLanguage")).toBe("nl");
  });

  it("lifts the cues while the controls are shown", async () => {
    vi.useFakeTimers();
    const { state } = await mountPlayer();
    const track = new FakeTrack("en");
    const cue = { line: "auto", snapToLines: true, lineAlign: "start" };
    track.cues = [cue];
    state.textTracks.add(track);
    await flushPromises();
    track.dispatchEvent(new Event("cuechange"));
    expect(cue).toMatchObject({ line: 78, snapToLines: false, lineAlign: "end" });
    await vi.advanceTimersByTimeAsync(3000);
    expect(cue).toMatchObject({ line: "auto", snapToLines: true, lineAlign: "start" });
  });

  describe("subtitle delay", () => {
    const makeCue = (start: number, end: number) => ({ line: "auto", snapToLines: true, lineAlign: "start", startTime: start, endTime: end });

    async function showing() {
      vi.useFakeTimers();
      const { state } = await mountPlayer();
      const track = new FakeTrack("en");
      const cues = [makeCue(10, 12), makeCue(20, 22)];
      track.cues = cues;
      state.textTracks.add(track);
      await flushPromises();
      track.mode = "showing";
      state.textTracks.dispatchEvent(new Event("change"));
      await flushPromises();
      return { state, track, cues };
    }

    it("shifts the cues with ] and [ and says so on the picture", async () => {
      const { cues } = await showing();
      press("]");
      press("]");
      await flushPromises();
      expect(cues.map((c) => [c.startTime, c.endTime])).toEqual([[10.5, 12.5], [20.5, 22.5]]);
      expect(wrapper!.find(".player-note").text()).toBe("Subtitles 0.5 s later");
      press("[");
      press("[");
      press("[");
      await flushPromises();
      expect(cues[0].startTime).toBe(9.75);
      expect(wrapper!.find(".player-note").text()).toBe("Subtitles 0.25 s earlier");
    });

    it("catches cues that arrive later and remembers the delay for the file", async () => {
      const { track, cues } = await showing();
      press("]");
      const late = makeCue(30, 32);
      (track.cues as unknown[]).push(late);
      await vi.advanceTimersByTimeAsync(1100);
      expect(late.startTime).toBe(30.25);
      expect(cues[0].startTime).toBe(10.25);
      expect(storageGet(`subtitleDelay:${MAGNET}|${FILE}`)).toBe("0.25");
    });

    it("starts a file with the delay it was left with, and never moves a cue before time zero", async () => {
      storageSet(`subtitleDelay:${MAGNET}|${FILE}`, "-3");
      vi.useFakeTimers();
      const { state } = await mountPlayer();
      const track = new FakeTrack("en");
      const early = makeCue(1, 2);
      track.cues = [early, makeCue(10, 12)];
      state.textTracks.add(track);
      await flushPromises();
      await vi.advanceTimersByTimeAsync(1100);
      expect([early.startTime, early.endTime]).toEqual([0, 0]);
      expect((track.cues[1] as ReturnType<typeof makeCue>).startTime).toBe(7);
    });

    it("does nothing while the subtitles are off", async () => {
      vi.useFakeTimers();
      const { state } = await mountPlayer();
      const track = new FakeTrack("en");
      const cue = makeCue(10, 12);
      track.cues = [cue];
      state.textTracks.add(track);
      await flushPromises();
      press("]");
      await flushPromises();
      expect(cue.startTime).toBe(10);
      expect(wrapper!.find(".player-note").exists()).toBe(false);
    });
  });

  it("renders the <track> elements it is given", async () => {
    await mountPlayer({ subtitles: [{ language: "eng", url: `/play/${HASH}/a_eng.vtt` }, { language: "nld", url: `/play/${HASH}/a_nld.vtt` }] });
    const tracks = wrapper!.findAll("track");
    expect(tracks.map((t) => t.attributes("src"))).toEqual([`/play/${HASH}/a_eng.vtt`, `/play/${HASH}/a_nld.vtt`]);
    expect(tracks[1].attributes("srclang")).toBe("nld");
  });
});

describe("hls early playback", () => {
  beforeEach(() => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  it("plays the stream through hls.js, resuming through startPosition", async () => {
    saveVideoPosition(MAGNET, FILE, 77, 0);
    const { video } = await mountPlayer({ url: HLS });
    const hls = FakeHls.last();
    expect(hls.config).toMatchObject({ startPosition: 77, enableCEA708Captions: false });
    expect(hls.loaded).toEqual([window.location.origin + HLS]);
    expect(hls.attached).toBe(video);
    expect(video.getAttribute("src")).toBeNull();
  });

  it("falls back to the plain src when hls.js is not supported", async () => {
    FakeHls.supported = false;
    const { video } = await mountPlayer({ url: HLS });
    expect(FakeHls.instances).toHaveLength(0);
    expect(video.getAttribute("src")).toBe(window.location.origin + HLS);
  });

  it("reloads the source in place when the subtitle count changes, keeping time and play state", async () => {
    const { state } = await mountPlayer({ url: HLS });
    const hls = FakeHls.last();
    state.currentTime = 42;
    state.paused = false;
    const next = `/play/${HASH}/hls%2Fmaster.m3u8?subs=2`;
    await wrapper!.setProps({ url: next });
    expect(hls.loaded).toEqual([window.location.origin + HLS, window.location.origin + next]);
    expect(FakeHls.instances).toHaveLength(1);
    state.currentTime = 0; // the reload resets the element
    state.paused = true;
    hls.emit("hlsManifestParsed");
    expect(state.currentTime).toBe(42);
    expect(state.paused).toBe(false);
  });

  it("a different path is not an in-place reload", async () => {
    await mountPlayer({ url: HLS });
    const hls = FakeHls.last();
    await wrapper!.setProps({ url: `/play/${HASH}/other.m3u8` });
    expect(hls.loaded).toHaveLength(1);
  });

  it("hands over to the MP4 at the same position", async () => {
    const hlsPlayer = await mountPlayer({ url: HLS });
    hlsPlayer.state.currentTime = 123.4;
    hlsPlayer.state.paused = false;
    hlsPlayer.state.duration = 900; // only the part converted so far
    wrapper!.unmount(); // what DownloadScreen's keyed swap does
    expect(getVideoPosition(MAGNET, FILE)).toBe(123.4);
    const mp4 = await mountPlayer({ url: MP4 });
    mp4.video.dispatchEvent(new Event("loadedmetadata"));
    expect(mp4.state.currentTime).toBe(123.4);
  });

  it("does not treat the end of a partial stream as watched, nor advance to the next file", async () => {
    responses["/api/next_file/"] = { next_filename: "Next.mkv", next_magnet: null };
    const { video, state, router } = await mountPlayer({ url: HLS });
    const hls = FakeHls.last();
    state.duration = 600;
    state.currentTime = 599;
    video.dispatchEvent(new Event("timeupdate"));
    expect(readWatchLog().some((e) => e.event === "watched")).toBe(false);
    const before = router.currentRoute.value.fullPath;
    video.dispatchEvent(new Event("ended"));
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe(before);
    expect(readWatchLog().some((e) => e.event === "watched")).toBe(false);
    // The playlist is finished: now the length counts.
    hls.emit("hlsLevelUpdated", { details: { live: false } });
    video.dispatchEvent(new Event("timeupdate"));
    expect(readWatchLog().some((e) => e.event === "watched")).toBe(true);
  });

  it("recovers from fatal media and network errors, then gives up after 5", async () => {
    await mountPlayer({ url: HLS });
    const hls = FakeHls.last();
    hls.emit("hlsError", { fatal: false, type: "networkError" });
    expect(hls.startLoad).not.toHaveBeenCalled();
    hls.emit("hlsError", { fatal: true, type: "mediaError", details: "x" });
    expect(hls.recoverMediaError).toHaveBeenCalledTimes(1);
    hls.emit("hlsError", { fatal: true, type: "networkError", details: "x" });
    expect(hls.startLoad).toHaveBeenCalledTimes(1);
    for (let i = 0; i < 3; i++) hls.emit("hlsError", { fatal: true, type: "networkError", details: "x" });
    expect(wrapper!.emitted("stream-error")).toBeUndefined();
    hls.emit("hlsError", { fatal: true, type: "networkError", details: "x" });
    expect(hls.destroy).toHaveBeenCalledTimes(1);
    expect(wrapper!.emitted("stream-error")).toHaveLength(1);
    hls.emit("hlsError", { fatal: true, type: "networkError", details: "x" });
    expect(wrapper!.emitted("stream-error")).toHaveLength(1);
  });

  it("asks for the playlist again when it never arrived, instead of waiting for fragments", async () => {
    await mountPlayer({ url: HLS });
    const hls = FakeHls.last();
    hls.emit("hlsError", { fatal: true, type: "networkError", details: "manifestLoadError" });
    expect(hls.startLoad).not.toHaveBeenCalled();
    expect(hls.loaded).toEqual([window.location.origin + HLS, window.location.origin + HLS]);
  });

  it("an unrecoverable error type emits stream-error at once", async () => {
    await mountPlayer({ url: HLS });
    const hls = FakeHls.last();
    hls.emit("hlsError", { fatal: true, type: "otherError", details: "x" });
    expect(hls.destroy).toHaveBeenCalled();
    expect(wrapper!.emitted("stream-error")).toHaveLength(1);
  });
});

describe("cleanup", () => {
  it("destroys hls, pauses, saves the position and removes every listener", async () => {
    vi.useFakeTimers();
    const { state } = await mountPlayer({ url: HLS });
    const hls = FakeHls.last();
    state.paused = false;
    state.currentTime = 250;
    state.duration = 800;
    const removed = vi.spyOn(document, "removeEventListener");
    wrapper!.unmount();
    wrapper = null;
    expect(hls.destroy).toHaveBeenCalled();
    expect(state.pauseCalls).toBeGreaterThan(0);
    expect(getVideoPosition(MAGNET, FILE)).toBe(250);
    expect(removed.mock.calls.map((c) => c[0])).toEqual(expect.arrayContaining(["mousemove", "touchstart", "click"]));
    // No keys, no timers any more.
    state.currentTime = 300;
    press("ArrowRight");
    expect(state.currentTime).toBe(300);
    const before = fetched.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetched.length).toBe(before);
    expect(getVideoPosition(MAGNET, FILE)).toBe(250);
  });

  it("does not save a position for a file that was just finished", async () => {
    const { video, state } = await mountPlayer();
    state.duration = 3000;
    state.currentTime = 2999;
    video.dispatchEvent(new Event("timeupdate"));
    wrapper!.unmount();
    wrapper = null;
    expect(getVideoPosition(MAGNET, FILE)).toBe(0);
  });

  it("a player torn down before hls.js loaded does not leave an instance running", async () => {
    const router = createAppRouter(createMemoryHistory("/"));
    wrapper = mount(PlayerView, {
      props: { url: HLS, supported: true, subtitles: [], magnet: MAGNET, filename: FILE, downloadProgress: null, downloadStatus: null },
      global: { plugins: [router] },
    });
    wrapper.unmount();
    wrapper = null;
    await flushPromises();
    for (const hls of FakeHls.instances) expect(hls.destroy).toHaveBeenCalled();
  });
});

describe("tapping the picture", () => {
  it("only toggles the controls on a touch screen, and pauses on a mouse click", async () => {
    const { video } = await mountPlayer();
    const play = vi.spyOn(video, "play").mockResolvedValue();
    const pause = vi.spyOn(video, "pause").mockImplementation(() => {});
    const el = wrapper!.find("video");
    const tap = async () => {
      await new Promise((r) => setTimeout(r, 120)); // reveal() ignores events closer than 100 ms
      await el.trigger("touchstart");
      await el.trigger("click", { pointerType: "touch" });
    };
    expect(wrapper!.find(".player-controls").isVisible()).toBe(true);
    await tap();
    expect(wrapper!.find(".player-controls").isVisible()).toBe(false);
    await tap();
    expect(wrapper!.find(".player-controls").isVisible()).toBe(true);
    expect(play).not.toHaveBeenCalled();
    expect(pause).not.toHaveBeenCalled();
  });
});

describe("buffering indicator", () => {
  it("shows once a wait has lasted a moment and goes when playback resumes", async () => {
    const { video } = await mountPlayer();
    await video.dispatchEvent(new Event("waiting"));
    await new Promise((r) => setTimeout(r, 100));
    expect(wrapper!.find(".player-buffering").exists()).toBe(false); // a short wait shows nothing
    await new Promise((r) => setTimeout(r, 450));
    await flushPromises();
    expect(wrapper!.find(".player-buffering").exists()).toBe(true);
    await video.dispatchEvent(new Event("playing"));
    await flushPromises();
    expect(wrapper!.find(".player-buffering").exists()).toBe(false);
  });
});

describe("chromecast button", () => {
  const chromeUa = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

  it("is hidden outside Chrome", async () => {
    await mountPlayer();
    expect(wrapper!.find(".chromecast-btn").exists()).toBe(false);
  });

  it("is offered on an Android phone in Chrome", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(chromeUa + " Mobile");
    await mountPlayer();
    expect(wrapper!.find(".chromecast-btn").exists()).toBe(true);
  });

  it("is hidden for a file that cannot be played", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(chromeUa);
    await mountPlayer({ supported: false });
    expect(wrapper!.find(".chromecast-btn").exists()).toBe(false);
  });

  it("is shown in desktop Chrome and a failed script load makes it vanish quietly", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(chromeUa);
    const { default: CastButton } = await import("./CastButton.vue");
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const shown = mount(CastButton, { props: { videoUrl: MP4, mediaTitle: "t", video: null, loader: () => new Promise(() => {}) } });
    expect(shown.find(".chromecast-btn").exists()).toBe(true); // while loading
    const failed = mount(CastButton, { props: { videoUrl: MP4, mediaTitle: "t", video: null, loader: async () => null } });
    await flushPromises();
    expect(failed.find(".chromecast-btn").exists()).toBe(false);
    expect(error).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
    shown.unmount();
    failed.unmount();
  });

  it("explains that localhost cannot be cast to, then the message goes away", async () => {
    vi.useFakeTimers();
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(chromeUa);
    const { default: CastButton } = await import("./CastButton.vue");
    const video = document.createElement("video");
    const button = mount(CastButton, { props: { videoUrl: MP4, mediaTitle: "t", video, loader: async () => ({ cast: vi.fn() }) } });
    await flushPromises();
    await button.find(".chromecast-btn").trigger("click");
    await flushPromises();
    expect(button.find(".cast-error").text()).toContain("network address");
    await vi.advanceTimersByTimeAsync(6100);
    expect(button.find(".cast-error").exists()).toBe(false);
    button.unmount();
  });
});

describe("episodes menu", () => {
  it("lists the episodes of the torrent and opens another one", async () => {
    responses["/api/magnet/"] = {
      files: ["Show.S01E01.1080p.mp4", "Show.S01E02.1080p.mp4", "Show.S01E03.1080p.mp4", "Other.Thing.S01E01.720p.mp4", "readme.txt"],
    };
    responses["/api/library/"] = { titles: [] };
    responses["/api/tv/lookup/"] = {};
    const { router } = await mountPlayer();
    await wrapper!.find('[aria-label="Episodes"]').trigger("click");
    await flushPromises();
    await flushPromises();
    const episodes = wrapper!.findAll(".player-episode");
    expect(episodes.map((e) => e.attributes("data-episode"))).toEqual(["1", "2", "3"]);
    expect(episodes[0].classes()).toContain("current");
    expect(document.activeElement).toBe(episodes[0].element);
    await episodes[2].trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe(magnetPath(MAGNET, "Show.S01E03.1080p.mp4"));
    expect(sessionStorage.getItem("rapidbayEpisodeTarget")).toBe(JSON.stringify({ season: 1, episode: 3 }));
  });

  it("details arriving later do not pull focus back to the playing episode", async () => {
    responses["/api/magnet/"] = { files: ["Show.S01E01.1080p.mp4", "Show.S01E02.1080p.mp4", "Show.S01E03.1080p.mp4"] };
    responses["/api/library/"] = { titles: [{ media_type: "tv", title: "Show", tmdb_id: 7 }] };
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    responses["/api/tv/7/season/1/"] = { episodes: [{ episode_number: 2, name: "The Second", overview: "plot", still_url: "" }] };
    const realFetch = globalThis.fetch;
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      if (url.startsWith("/api/tv/7/season/")) await gate;
      return realFetch(url, init);
    });
    await mountPlayer();
    await wrapper!.find('[aria-label="Episodes"]').trigger("click");
    await flushPromises();
    const items = () => wrapper!.findAll(".player-episode");
    expect(items()).toHaveLength(3);
    items()[0].element.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(items()[1].element);
    release();
    await flushPromises();
    await flushPromises();
    expect(wrapper!.text()).toContain("The Second");
    expect(document.activeElement).toBe(items()[1].element);
    expect(items()[1].classes()).toContain("focused");
  });

  it("arrow keys move through the list", async () => {
    responses["/api/magnet/"] = { files: ["Show.S01E01.1080p.mp4", "Show.S01E02.1080p.mp4"] };
    responses["/api/library/"] = { titles: [] };
    responses["/api/tv/lookup/"] = {};
    await mountPlayer();
    await wrapper!.find('[aria-label="Episodes"]').trigger("click");
    await flushPromises();
    await flushPromises();
    const items = wrapper!.findAll(".player-episode");
    expect(document.activeElement).toBe(items[0].element);
    items[0].element.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(items[1].element);
  });
});
