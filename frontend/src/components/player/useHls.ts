// hls.js wiring: the stream is played while the file is still being encoded, at the
// quality the connection allows. hls.js is a separate chunk, loaded only for .m3u8 links.
import type HlsType from "hls.js";

export const MAX_HLS_RECOVERIES = 5;

export async function loadHls(): Promise<typeof HlsType> {
  return (await import("hls.js")).default;
}

export interface QualityLevel {
  index: number;
  height: number;
}

export interface AudioChoice {
  index: number;
  label: string;
}

export interface HlsHandle {
  // Swap the playlist (new subtitles arrived) and keep the position and play state.
  reload(url: string): void;
  // Pick a rendition by index, or -1 to let the player choose from the connection speed.
  setQuality(index: number): void;
  // Switch to another audio track of the stream, by its index in the playlist.
  setAudioTrack(index: number): void;
  destroy(): void;
}

export interface HlsOptions {
  startPosition: number;
  onFatal: () => void;
  // Whether the playlist is still growing; the length is unknown until it is not.
  onLive: (live: boolean) => void;
  // The renditions the stream offers, best first. Empty or one entry: nothing to choose.
  onLevels?: (levels: QualityLevel[]) => void;
  // The rendition currently playing (it changes as the connection does).
  onLevelSwitch?: (index: number) => void;
  // The audio tracks the stream offers (languages), and the one playing. One track or none: nothing to choose.
  onAudioTracks?: (tracks: AudioChoice[]) => void;
  onAudioSwitch?: (index: number) => void;
}

function debugRequested(): boolean {
  try {
    return window.localStorage.getItem("hlsDebug") === "1";
  } catch {
    return false;
  }
}

// Segments loaded at the starting quality before the player may change it.
const START_LEVEL_HOLD_FRAGMENTS = 3;
// The quality to start at: the best one that needs no more than this, so a first look is quick on most links.
const START_BITRATE = 2_500_000;

// Index of the level to start on: the best whose bitrate fits START_BITRATE, else the lowest. -1 if none.
export function startLevelFor(levels: { bitrate?: number; height?: number }[]): number {
  if (!levels.length) return -1;
  let best = -1;
  levels.forEach((level, index) => {
    const bitrate = level.bitrate || 0;
    if (bitrate <= START_BITRATE && (best < 0 || (level.height || 0) > (levels[best].height || 0))) best = index;
  });
  if (best >= 0) return best;
  let lowest = 0;
  levels.forEach((level, index) => {
    if ((level.bitrate || Infinity) < (levels[lowest].bitrate || Infinity)) lowest = index;
  });
  return lowest;
}

export function playSafely(video: HTMLVideoElement): void {
  try {
    const promise = video.play() as Promise<void> | undefined;
    if (promise && typeof promise.catch === "function") promise.catch(() => {});
  } catch {
    // Autoplay refused; the user presses play.
  }
}

// Resolves to null when hls.js cannot run here (the caller then uses the
// browser's native HLS, or the plain src).
export async function attachHls(video: HTMLVideoElement, url: string, options: HlsOptions): Promise<HlsHandle | null> {
  const Hls = await loadHls();
  if (!Hls.isSupported()) return null;
  const hls = new Hls({
    // localStorage.hlsDebug = "1" prints hls.js's own log to the console, for finding out why a stream is stuck.
    debug: debugRequested(),
    // Resume through hls.js itself: setting currentTime before the manifest is
    // parsed races its own initial seek (the live edge for EVENT playlists).
    // 0 is an explicit start at the beginning.
    startPosition: options.startPosition > 0 ? options.startPosition : 0,
    maxBufferHole: 0.5,
    nudgeOffset: 0.2,
    nudgeMaxRetry: 10,
    // Subtitles come from EXT-X-MEDIA in the master playlist; skip the
    // embedded CEA-608/708 captions so they do not render alongside.
    enableCEA708Captions: false,
    // Adaptive bitrate starts from a bandwidth estimate. The level is not capped to the player's size: resizing
    // the window or leaving fullscreen would then switch renditions mid-playback, and a switch to a variant whose
    // next segments are not encoded yet leaves the picture frozen until the encoder gets there.
    abrEwmaDefaultEstimate: 2_000_000,
  });
  let dead = false;
  let recoveries = 0;
  let holdingStartLevel = false;
  let startFragments = 0;
  const giveUp = () => {
    if (dead) return;
    dead = true;
    hls.destroy();
    options.onFatal();
  };
  hls.on(Hls.Events.ERROR, (_event, data) => {
    if (dead || !data.fatal) return;
    console.warn("HLS fatal error:", data.type, data.details);
    recoveries++;
    if (recoveries > MAX_HLS_RECOVERIES) giveUp();
    else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
    else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      // startLoad() only resumes fragment loading; a playlist that never
      // arrived has to be requested again, or playback would hang with no
      // error ever reaching the fallback below.
      if (/^manifest/.test(String(data.details))) hls.loadSource(currentUrl);
      else hls.startLoad();
    }
    else giveUp();
  });
  hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
    const levels = (data.levels || []).map((level, index) => ({ index, height: level.height || 0 }));
    options.onLevels?.([...levels].sort((a, b) => b.height - a.height));
    // The first segment of a session waits for the encoder to start, so timing it says nothing about the
    // connection: a quality switch made on that reading leaves the player with nothing to show. Hold the
    // starting level until a few segments are in, then let the connection decide.
    const start = startLevelFor(data.levels || []);
    if (start >= 0 && data.levels.length > 1) {
      hls.startLevel = start;
      hls.currentLevel = start;
      holdingStartLevel = true;
    }
  });
  hls.on(Hls.Events.FRAG_BUFFERED, (_event, data) => {
    if (!holdingStartLevel || data.frag.type !== "main") return;
    if (++startFragments >= START_LEVEL_HOLD_FRAGMENTS) {
      holdingStartLevel = false;
      hls.currentLevel = -1; // adaptive from here on
    }
  });
  hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, (_event, data) => {
    options.onAudioTracks?.((data.audioTracks || []).map((track, index) => ({ index, label: track.name || track.lang || "Track " + (index + 1) })));
    options.onAudioSwitch?.(hls.audioTrack);
  });
  hls.on(Hls.Events.AUDIO_TRACK_SWITCHED, (_event, data) => options.onAudioSwitch?.(data.id));
  hls.on(Hls.Events.LEVEL_SWITCHED, (_event, data) => options.onLevelSwitch?.(data.level));
  hls.on(Hls.Events.LEVEL_UPDATED, (_event, data) => {
    options.onLive(!!(data && data.details && data.details.live));
  });
  let currentUrl = url;
  hls.loadSource(url);
  hls.attachMedia(video);
  return {
    reload(newUrl: string) {
      if (dead) return;
      const resumeAt = video.currentTime;
      const wasPlaying = !video.paused && !video.ended;
      hls.once(Hls.Events.MANIFEST_PARSED, () => {
        if (resumeAt > 0) video.currentTime = resumeAt;
        if (wasPlaying) playSafely(video);
      });
      currentUrl = newUrl;
      hls.loadSource(newUrl);
    },
    setQuality(index: number) {
      if (dead) return;
      holdingStartLevel = false; // the viewer has chosen: the starting hold must not undo it
      hls.currentLevel = index;
    },
    setAudioTrack(index: number) {
      if (!dead) hls.audioTrack = index;
    },
    destroy() {
      if (dead) return;
      dead = true;
      hls.destroy();
    },
  };
}
