<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { get, post } from "../../lib/api";
import type { SubtitleTrack } from "../../lib/download";
import { toggleFullscreen } from "../../lib/fullscreen";
import {
  clearVideoPosition,
  getVideoPosition,
  markFileCompleted,
  progressCountsAsWatched,
  reportPlaybackProgress,
  saveVideoPosition,
} from "../../lib/library";
import { get_hash, magnetPath, rememberEpisodeTarget, torrentPath } from "../../lib/magnet";
import FavoriteButton from "../FavoriteButton.vue";
import CaptionButton from "./CaptionButton.vue";
import CastButton from "./CastButton.vue";
import EpisodeMenu from "./EpisodeMenu.vue";
import TracksMenu from "./TracksMenu.vue";
import { cleanMediaTitle, formatClock, languageName, type EpisodeChoice } from "./media";
import { attachHls, playSafely, type AudioChoice, type HlsHandle, type QualityLevel } from "./useHls";
import { useEpisodes } from "./useEpisodes";
import { usePlayerKeys } from "./usePlayerKeys";
import { useTextTracks } from "./useTextTracks";

const props = defineProps<{
  url: string;
  supported: boolean | null;
  subtitles: SubtitleTrack[];
  magnet: string;
  filename: string;
  downloadProgress: number | null;
  downloadStatus: string | null;
}>();
const emit = defineEmits<{ back: []; "stream-error": [] }>();

const router = useRouter();

const HIDE_AFTER_MS = 2800;
const POSITION_SAVE_MS = 5000;
const isChrome = /Chrome/i.test(navigator.userAgent);

const video = ref<HTMLVideoElement | null>(null);
const hovering = ref(true);
const openMenu = ref<"" | "episodes" | "tracks">("");
const paused = ref(true);
// The picture is waiting for data (starting, seeking, or the connection fell behind the encoder).
const buffering = ref(false);
let bufferingTimer: ReturnType<typeof setTimeout> | undefined;
const currentTime = ref(0);
const duration = ref(0);
const volume = ref(1);
const muted = ref(false);
const nextFilename = ref("");
const nextMagnet = ref<string | null>(null);

const controlsOn = computed(() => hovering.value || !!openMenu.value);
// The torrent of the file being played (its play URL carries it only for a plain /play/ link).
const hash = computed(() => get_hash(props.magnet));
const showCast = computed(() => !!props.supported && !!props.url && isChrome);
const fileLink = computed(() => window.location.origin + props.url.split("?")[0]);
const displayTitle = computed(() => cleanMediaTitle(props.filename));
const statusLabel = computed(() => {
  const text = (props.downloadStatus || "").replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
});
const elapsedLabel = computed(() => formatClock(currentTime.value));
const totalLabel = computed(() => (duration.value ? formatClock(duration.value) : "0:00"));
const remainingLabel = computed(() => (duration.value ? formatClock(Math.max(0, duration.value - currentTime.value)) : "0:00"));
// How far the browser has loaded ahead of the playhead, as a share of the whole length.
const bufferedEnd = ref(0);
const bufferedPercent = computed(() => (duration.value ? Math.min(100, (bufferedEnd.value / duration.value) * 100) : 0) + "%");
// While the file downloads, the part that has arrived is all that can be played: a marker shows where it ends.
// It is bytes over bytes, so it matches the timeline only roughly, more so for a variable-bitrate file.
const downloadLimit = computed(() =>
  props.downloadProgress !== null && props.downloadProgress < 1 ? Math.max(0, Math.min(1, props.downloadProgress)) * 100 : null,
);
const scrubValue = computed(() => (duration.value ? Math.round((currentTime.value / duration.value) * 1000) : 0));
const progressPercent = computed(() => scrubValue.value / 10 + "%");

// Captions sit just above the controls: as a percentage of the video box, so
// it depends on how tall the controls are (they wrap on a phone).
function raisedCueLine(): number {
  const box = video.value ? video.value.clientHeight : 0;
  const controls = root.value?.querySelector<HTMLElement>(".player-controls")?.offsetHeight || 0;
  if (!box || !controls) return 78;
  return Math.max(40, Math.min(90, Math.round((1 - (controls + 12) / box) * 100)));
}
const tracks = useTextTracks(video, () => controlsOn.value, raisedCueLine);
const episodes = useEpisodes({ hash: () => hash.value, magnet: () => props.magnet, filename: () => props.filename });
watch(controlsOn, (shown) => tracks.placeCues(shown));

// ---- controls auto-hide --------------------------------------------------------------

let hideTimer: ReturnType<typeof setTimeout> | undefined;
let lastReveal = 0;

function reveal(): void {
  const now = Date.now();
  if (now - lastReveal < 100) return;
  lastReveal = now;
  hovering.value = true;
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => {
    if (openMenu.value) return;
    hovering.value = false;
    // Blur so a focused control does not keep catching key presses.
    if (video.value) video.value.blur();
  }, HIDE_AFTER_MS);
}

// ---- playback ---------------------------------------------------------------------

function syncBuffered(): void {
  const el = video.value;
  if (!el || !el.buffered) return;
  const at = el.currentTime || 0;
  let end = 0;
  for (let i = 0; i < el.buffered.length; i++) {
    // The range holding the playhead is the one that counts; a gap after a seek starts a new range.
    if (el.buffered.start(i) <= at + 0.5 && el.buffered.end(i) >= at) end = el.buffered.end(i);
  }
  bufferedEnd.value = end;
}

function syncPlayback(): void {
  const el = video.value;
  if (!el) return;
  syncBuffered();
  paused.value = el.paused;
  currentTime.value = el.currentTime || 0;
  duration.value = isFinite(el.duration) ? el.duration : 0;
  volume.value = el.volume;
  muted.value = el.muted || el.volume === 0;
}

function playPause(wanted?: "play" | "pause"): void {
  const el = video.value;
  if (!el) return;
  if (wanted === "play" || (wanted !== "pause" && el.paused)) playSafely(el);
  else el.pause();
}

// A tap on a touch screen shows or hides the controls (play and pause are on the
// controls); only a mouse click on the picture toggles playback.
let tapFoundControlsHidden = false;
function onVideoTouchStart(): void {
  tapFoundControlsHidden = !controlsOn.value;
}
function onVideoClick(event: MouseEvent): void {
  if ((event as PointerEvent).pointerType !== "touch") return togglePlay();
  if (tapFoundControlsHidden || openMenu.value) {
    openMenu.value = "";
    return;
  }
  clearTimeout(hideTimer);
  hovering.value = false;
}

function togglePlay(): void {
  openMenu.value = "";
  playPause();
}

function seekBy(seconds: number): void {
  const el = video.value;
  if (!el) return;
  let next = el.currentTime + seconds;
  if (isFinite(el.duration)) next = Math.min(el.duration, next);
  el.currentTime = Math.max(0, next);
  syncPlayback();
}

function onScrub(event: Event): void {
  const el = video.value;
  if (!el || !isFinite(el.duration) || el.duration <= 0) return;
  el.currentTime = (Number((event.target as HTMLInputElement).value) / 1000) * el.duration;
  syncPlayback();
}

function toggleMute(): void {
  const el = video.value;
  if (!el) return;
  el.muted = !el.muted;
  if (!el.muted && el.volume === 0) el.volume = 1;
  syncPlayback();
}

function setVolume(event: Event): void {
  const el = video.value;
  if (!el) return;
  el.volume = Number((event.target as HTMLInputElement).value);
  el.muted = el.volume === 0;
  syncPlayback();
}

// ---- menus ------------------------------------------------------------------------

function closeMenu(): void {
  const name = openMenu.value;
  openMenu.value = "";
  void nextTick(() => {
    const label = name === "episodes" ? "Episodes" : "Quality, audio and subtitles";
    root.value?.querySelector<HTMLElement>(`[aria-label="${label}"]`)?.focus();
  });
}

function toggleMenu(name: "episodes" | "tracks"): void {
  hovering.value = true;
  openMenu.value = openMenu.value === name ? "" : name;
  if (openMenu.value === "episodes") void episodes.load();
}

const root = ref<HTMLElement | null>(null);

// ---- navigation to other files ----------------------------------------------------------

function go(path: string): void {
  void router.replace(path);
}

function playEpisode(episode: EpisodeChoice): void {
  if (episode.current) {
    openMenu.value = "";
    return;
  }
  rememberEpisodeTarget(episode.season, episode.episode);
  let path = "";
  if (episode.filename && episode.magnet) path = magnetPath(episode.magnet, episode.filename);
  else if (episode.magnet) path = magnetPath(episode.magnet);
  else if (episode.torrentLink) path = torrentPath(episode.torrentLink);
  if (!path) return;
  openMenu.value = "";
  go(path);
}

interface NextFile {
  next_filename?: string | null;
  next_magnet?: string | null;
}

async function fetchNextFile(): Promise<NextFile | null> {
  try {
    return await get<NextFile>("/api/next_file/" + hash.value + "/" + encodeURIComponent(props.filename));
  } catch {
    return null;
  }
}

function rememberNext(data: NextFile | null): void {
  if (data && data.next_filename) {
    nextFilename.value = data.next_filename;
    nextMagnet.value = data.next_magnet || null;
  }
}

function playNext(): void {
  if (!nextFilename.value) return;
  go(magnetPath(nextMagnet.value || props.magnet, nextFilename.value));
}

// Start fetching the next file as soon as playback starts, once per file.
let prefetched = "";
async function onPlay(): Promise<void> {
  syncPlayback();
  const data = await fetchNextFile();
  if (disposed) return;
  rememberNext(data);
  if (data && data.next_filename && prefetched !== data.next_filename) {
    prefetched = data.next_filename;
    try {
      await post("/api/magnet_download/", { magnet_link: data.next_magnet || props.magnet, filename: data.next_filename });
    } catch {
      // The next file just starts when it is opened.
    }
  }
}

async function onEnded(): Promise<void> {
  // A stream that is still converting only "ends" at the end of what exists.
  if (streamLive) return;
  noteWatched();
  const data = await fetchNextFile();
  if (disposed || !data || !data.next_filename) return;
  go(magnetPath(data.next_magnet || props.magnet, data.next_filename));
}

// ---- watched progress ----------------------------------------------------------------------

let disposed = false;
let notedWatch = false;
// While a stream is still being converted, video.duration is only the length
// written so far. It counts as unknown until hls.js reports a finished
// playlist, so the end of that partial length never counts as the end.
let streamLive = false;
let positionTimer: ReturnType<typeof setInterval> | undefined;
let hls: HlsHandle | null = null;
const qualityLevels = ref<QualityLevel[]>([]);
// Audio tracks of an HLS stream come from hls.js; the browser's own list only exists in Safari.
const hlsAudio = ref<AudioChoice[]>([]);
const hlsAudioActive = ref<number | null>(null);
const audioChoices = computed(() => (hlsAudio.value.length > 1 ? hlsAudio.value : tracks.audioChoices.value));
const activeAudio = computed(() => (hlsAudio.value.length > 1 ? hlsAudioActive.value : tracks.activeAudio.value));

function selectAudio(index: number): void {
  if (hlsAudio.value.length > 1 && hls) {
    hls.setAudioTrack(index);
    hlsAudioActive.value = index;
  } else {
    tracks.selectAudio(index);
  }
}
const selectedQuality = ref(-1);
const playingQuality = ref(-1);

function selectQuality(index: number): void {
  selectedQuality.value = index;
  hls?.setQuality(index);
}

function knownDuration(): number {
  const el = video.value;
  return streamLive || !el ? 0 : el.duration;
}

function noteWatched(): void {
  if (notedWatch) return;
  notedWatch = true;
  clearVideoPosition(props.magnet, props.filename);
  markFileCompleted(props.magnet, props.filename);
}

function onTimeUpdate(): void {
  syncPlayback();
  const el = video.value;
  // Credits usually start before the file ends: count it at 98% or with under
  // two minutes left, whichever comes first.
  if (el && progressCountsAsWatched(el.currentTime, knownDuration())) noteWatched();
}

// A short wait is not worth a spinner, so it shows only once the wait has lasted a moment.
function startBuffering(): void {
  if (bufferingTimer !== undefined || buffering.value) return;
  bufferingTimer = setTimeout(() => {
    bufferingTimer = undefined;
    buffering.value = true;
  }, 400);
}

function stopBuffering(): void {
  clearTimeout(bufferingTimer);
  bufferingTimer = undefined;
  buffering.value = false;
}

function onPause(): void {
  syncPlayback();
  const el = video.value;
  if (disposed || notedWatch || !el) return;
  saveVideoPosition(props.magnet, props.filename, el.currentTime, knownDuration());
  reportPlaybackProgress(props.magnet, props.filename, el.currentTime, knownDuration(), true);
}

function onLoadedMetadata(): void {
  syncPlayback();
  tracks.refresh();
}

// ---- source ------------------------------------------------------------------------------

const fullUrl = (url: string) => window.location.origin + url;
const isHlsUrl = (url: string) => url.indexOf(".m3u8") !== -1;

function resumeAt(el: HTMLVideoElement, seconds: number, wasPlaying = true): void {
  const apply = () => {
    el.removeEventListener("loadedmetadata", apply);
    if (seconds > 0) el.currentTime = seconds;
    if (wasPlaying) playSafely(el);
  };
  el.addEventListener("loadedmetadata", apply);
}

async function startSource(): Promise<void> {
  const el = video.value;
  if (!el) return;
  const url = fullUrl(props.url);
  const saved = getVideoPosition(props.magnet, props.filename);
  streamLive = isHlsUrl(props.url);
  if (streamLive) {
    try {
      hls = await attachHls(el, url, {
        startPosition: saved,
        onFatal: () => {
          hls = null;
          if (!disposed) emit("stream-error");
        },
        onLive: (live) => (streamLive = live),
        onLevels: (levels) => (qualityLevels.value = levels),
        onLevelSwitch: (index) => (playingQuality.value = index),
        onAudioTracks: (list) => (hlsAudio.value = list),
        onAudioSwitch: (index) => (hlsAudioActive.value = index),
      });
    } catch {
      hls = null;
    }
    if (disposed) {
      hls?.destroy();
      hls = null;
      return;
    }
    if (!hls) {
      // No hls.js here (Safari plays HLS natively).
      el.src = url;
      if (saved > 0) resumeAt(el, saved, false);
    }
  } else {
    el.src = url;
    if (saved > 0) {
      resumeAt(el, saved, false);
      el.currentTime = saved;
    }
  }
  playSafely(el);
}

// The same stream with a new query string (?subs= grew): reload the source in
// place so subtitles appear without remounting the <video>.
watch(
  () => props.url,
  (next, old) => {
    const el = video.value;
    if (!el || !next || next === old || next.split("?")[0] !== old.split("?")[0]) return;
    if (hls) hls.reload(fullUrl(next));
    else if (el.canPlayType("application/vnd.apple.mpegurl")) {
      const at = el.currentTime;
      const wasPlaying = !el.paused && !el.ended;
      el.src = fullUrl(next);
      resumeAt(el, at, wasPlaying);
    }
  },
);

// ---- lifecycle ----------------------------------------------------------------------------

usePlayerKeys({
  playPause,
  seekBy,
  toggleMute,
  toggleFullscreen,
  cycleCaption: () => tracks.cycleCaption(),
  playNext,
  hasNext: () => !!nextFilename.value,
  menuOpen: () => !!openMenu.value,
  closeMenu,
  openRawVideo: () => {
    const src = video.value && (video.value.currentSrc || video.value.src);
    if (!src || /^blob:/.test(src)) return false;
    window.location.assign(src);
    return true;
  },
  reveal,
});

const REVEAL_EVENTS = ["mousemove", "touchstart", "click"] as const;

let started = false;

function begin(): void {
  if (started || !video.value) return;
  started = true;
  tracks.attach();
  void fetchNextFile().then(rememberNext);
  positionTimer = setInterval(() => {
    const el = video.value;
    if (!el || notedWatch || el.paused || !(el.currentTime > 0)) return;
    saveVideoPosition(props.magnet, props.filename, el.currentTime, knownDuration());
    reportPlaybackProgress(props.magnet, props.filename, el.currentTime, knownDuration(), false);
  }, POSITION_SAVE_MS);
  void startSource();
}

onMounted(() => {
  reveal();
  for (const name of REVEAL_EVENTS) document.addEventListener(name, reveal);
  begin();
});
// The <video> only exists once the file is known to be playable.
watch(video, begin);

// beforeUnmount, not unmounted: when the download screen swaps HLS for the MP4
// it must find this position saved before the new player reads it.
onBeforeUnmount(() => {
  disposed = true;
  clearTimeout(hideTimer);
  clearTimeout(bufferingTimer);
  clearInterval(positionTimer);
  for (const name of REVEAL_EVENTS) document.removeEventListener(name, reveal);
  episodes.dispose();
  tracks.detach();
  const el = video.value;
  if (el) {
    // Read the position before hls.destroy(): detaching the media resets it to 0.
    // A stream that is still converting has no known length yet, so the
    // duration is left out and the saved one (if any) is kept.
    const finalDuration = streamLive ? 0 : el.duration;
    if (!notedWatch && el.currentTime > 0 && !progressCountsAsWatched(el.currentTime, finalDuration)) {
      saveVideoPosition(props.magnet, props.filename, el.currentTime, finalDuration);
      reportPlaybackProgress(props.magnet, props.filename, el.currentTime, finalDuration, true);
    }
  }
  if (hls) hls.destroy();
  hls = null;
  if (el) el.pause();
});

</script>

<template>
  <main ref="root" class="player" :class="{ 'controls-hidden': !controlsOn }" data-screen="player-screen">
    <video
      v-if="supported && url"
      ref="video"
      class="player-video"
      poster="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
      disablepictureinpicture
      autoplay
      playsinline
      tabindex="0"
      :aria-label="displayTitle || 'Video'"
      @touchstart.passive="onVideoTouchStart"
      @click="onVideoClick"
      @loadstart="startBuffering"
      @waiting="startBuffering"
      @seeking="startBuffering"
      @playing="stopBuffering"
      @canplay="stopBuffering"
      @seeked="stopBuffering"
      @emptied="stopBuffering"
      @play="onPlay"
      @pause="onPause"
      @timeupdate="onTimeUpdate"
      @ended="onEnded"
      @loadedmetadata="onLoadedMetadata"
      @durationchange="syncPlayback"
      @progress="syncBuffered"
      @volumechange="syncPlayback"
    >
      <track
        v-for="subtitle in subtitles"
        :key="subtitle.url"
        :id="subtitle.url"
        :label="languageName(subtitle.language)"
        kind="subtitles"
        :srclang="subtitle.language"
        :src="subtitle.url"
      />
    </video>
    <div v-if="buffering && supported && url" class="player-buffering" role="status" aria-live="polite">
      <span class="player-buffering-ring" aria-hidden="true"></span>
      <span class="sr-only">Buffering</span>
    </div>
    <div v-if="!(supported && url)" class="player-unsupported" role="status">
      <p>This file's format can't be played in the browser.</p>
      <a :href="fileLink" download>Download the file</a>
    </div>

    <header v-show="controlsOn" class="player-top">
      <button type="button" class="icon-btn back-btn" aria-label="Back" title="Back" @click="emit('back')">
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M15 5l-7 7 7 7" /></svg>
      </button>
      <span v-if="downloadProgress !== null && downloadProgress < 1" class="player-badge" :title="statusLabel">
        {{ Math.round(downloadProgress * 100) }}%
        <span v-if="statusLabel" class="player-badge-status">{{ statusLabel }}</span>
      </span>
      <div v-if="supported" class="player-top-actions">
        <CastButton v-if="showCast" :video-url="url" :magnet-hash="get_hash(magnet)" :media-title="displayTitle" :video="video" />
        <FavoriteButton :magnet="magnet" :filename="filename" />
      </div>
    </header>

    <div v-if="supported" v-show="controlsOn" class="player-controls" @click.stop>
      <div class="player-progress">
        <span class="player-elapsed" aria-label="Elapsed time">{{ elapsedLabel }}</span>
        <div class="player-scrub-wrap">
          <div class="player-track" aria-hidden="true">
            <div v-if="downloadLimit !== null" class="player-downloaded" :style="{ width: downloadLimit + '%' }"></div>
            <div class="player-buffered" :style="{ width: bufferedPercent }"></div>
            <div
              v-if="downloadLimit !== null"
              class="player-limit"
              :style="{ left: downloadLimit + '%' }"
              :title="'Downloaded so far: ' + Math.round(downloadLimit) + '%'"
            ></div>
          </div>
          <input
            class="player-scrub"
            type="range"
            min="0"
            max="1000"
            :value="scrubValue"
            :style="{ '--progress': progressPercent }"
            aria-label="Seek"
            @input="onScrub"
          />
        </div>
        <span class="player-total" :title="'Time remaining ' + remainingLabel" aria-label="Total length">{{ totalLabel }}</span>
      </div>
      <div class="player-row">
        <button type="button" class="player-btn" :aria-label="paused ? 'Play' : 'Pause'" @click="togglePlay">
          <svg v-if="paused" class="solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" /></svg>
          <svg v-else class="solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h3v14H7zM14 5h3v14h-3z" /></svg>
        </button>
        <button type="button" class="player-btn" aria-label="Back 10 seconds" @click="seekBy(-10)">
          <svg class="player-skip" viewBox="0 0 64 64" aria-hidden="true">
            <path d="M23.6 16.1A18 18 0 1 0 35.1 14.3" /><polygon class="arrow" points="35,11 22,14 35,17" /><text x="32" y="38" text-anchor="middle">10</text>
          </svg>
        </button>
        <button type="button" class="player-btn" aria-label="Forward 10 seconds" @click="seekBy(10)">
          <svg class="player-skip" viewBox="0 0 64 64" aria-hidden="true">
            <path d="M40.4 16.1A18 18 0 1 1 28.9 14.3" /><polygon class="arrow" points="29,11 42,14 29,17" /><text x="32" y="38" text-anchor="middle">10</text>
          </svg>
        </button>
        <div class="player-volume">
          <button type="button" class="player-btn" :aria-label="muted ? 'Unmute' : 'Mute'" @click="toggleMute">
            <svg v-if="muted" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10v4h3l4 4V6L7 10H4zM16 9l5 6M21 9l-5 6" /></svg>
            <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10v4h3l4 4V6L7 10H4zM16 9a4 4 0 0 1 0 6M18.5 7a7 7 0 0 1 0 10" /></svg>
          </button>
          <input class="player-volume-slider" type="range" min="0" max="1" step="0.05" :value="muted ? 0 : volume" aria-label="Volume" @input="setVolume" />
        </div>
        <h1 class="player-title" :title="displayTitle">{{ displayTitle }}</h1>
        <button v-if="nextFilename" type="button" class="player-btn" aria-label="Next episode" @click="playNext">
          <svg class="solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5v14l9-7zM17 5h2v14h-2z" /></svg>
        </button>
        <CaptionButton :track-count="tracks.trackCount.value" :current="tracks.currentCaption.value" @cycle="tracks.cycleCaption" />
        <div class="player-pop">
          <button type="button" class="player-btn" aria-label="Episodes" :aria-expanded="openMenu === 'episodes'" @click="toggleMenu('episodes')">
            <svg class="solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14v3H5zM5 11h14v3H5zM5 15h14v3H5z" /></svg>
          </button>
          <EpisodeMenu
            v-if="openMenu === 'episodes'"
            :groups="episodes.groups.value"
            :loading="episodes.loading.value"
            :empty="!episodes.choices.value.length"
            :show-title="episodes.showTitle.value"
            :focused-key="episodes.focusedKey.value"
            @play="playEpisode"
            @focus="(e: EpisodeChoice) => (episodes.focusedKey.value = episodes.keyOf(e))"
          />
        </div>
        <div class="player-pop">
          <button type="button" class="player-btn" aria-label="Quality, audio and subtitles" :aria-expanded="openMenu === 'tracks'" @click="toggleMenu('tracks')">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H9l-4 3v-3H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2z" /><path d="M8 11h2M12 11h4M8 14h5" /></svg>
          </button>
          <TracksMenu
            v-if="openMenu === 'tracks'"
            :audio-choices="audioChoices"
            :active-audio="activeAudio"
            :caption-choices="tracks.captionChoices.value"
            :active-caption="tracks.activeCaption.value"
            :quality-levels="qualityLevels"
            :selected-quality="selectedQuality"
            :playing-quality="playingQuality"
            @quality="selectQuality"
            @audio="selectAudio"
            @caption="tracks.selectCaption"
          />
        </div>
        <button type="button" class="player-btn" aria-label="Full screen" title="Full screen" @click="toggleFullscreen">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" /></svg>
        </button>
      </div>
    </div>
  </main>
</template>

<style>
.player {
  position: fixed;
  inset: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: #000;
  color: var(--text);
}
.player.controls-hidden { cursor: none; }
.player-video { width: 100%; height: 100%; object-fit: contain; background: #000; outline: none; }
.player-video::cue { background: rgba(0, 0, 0, 0.6); font: 600 1.15em/1.3 system-ui, sans-serif; }
.player-buffering { position: absolute; inset: 0; display: grid; place-items: center; pointer-events: none; }
.player-buffering-ring {
  width: 56px;
  height: 56px;
  border: 5px solid rgba(255, 255, 255, 0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: player-buffering-rotate 0.9s linear infinite;
}
@keyframes player-buffering-rotate { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .player-buffering-ring { animation-duration: 3s; } }
.player-unsupported { display: grid; gap: var(--space-3); text-align: center; padding: var(--space-5); color: var(--text-dim); }
.player-unsupported a { color: var(--link); }

.player-top {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--gutter) var(--space-6);
  background: linear-gradient(rgba(0, 0, 0, 0.75), transparent);
}
.player-top .icon-btn { background: rgba(24, 26, 31, 0.72); backdrop-filter: blur(6px); }
.player-top-actions { display: flex; align-items: flex-start; gap: var(--space-2); margin-left: auto; }
.cast-wrap { position: relative; display: inline-flex; }
.cast-error {
  position: absolute;
  top: calc(100% + var(--space-2));
  right: 0;
  width: max-content;
  max-width: min(280px, 80vw);
  padding: var(--space-2) var(--space-3);
  font-size: 0.85rem;
  color: var(--danger-text);
  background: var(--surface);
  border: 1px solid var(--danger);
  border-radius: var(--radius);
  white-space: normal;
}
.player-badge {
  position: relative;
  padding: 4px 10px;
  font-size: 0.85rem;
  font-variant-numeric: tabular-nums;
  background: rgba(24, 26, 31, 0.72);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
}
.player-badge-status { margin-left: var(--space-2); color: var(--text-dim); }

.player-controls {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 2;
  padding: var(--space-6) var(--gutter) max(var(--space-4), env(safe-area-inset-bottom));
  background: linear-gradient(transparent, rgba(0, 0, 0, 0.82));
}
.player-progress { display: flex; align-items: center; gap: var(--space-3); }
.player-elapsed, .player-total { min-width: 3.5em; font-size: 0.85rem; font-variant-numeric: tabular-nums; color: var(--text-dim); }
.player-elapsed { text-align: left; }
.player-total { text-align: right; }
.player-scrub, .player-volume-slider { -webkit-appearance: none; appearance: none; background: transparent; cursor: pointer; margin: 0; }
.player-scrub-wrap { position: relative; flex: 1; min-width: 0; height: 28px; }
/* The bar is the whole length: a base track, what the browser has loaded, and (while downloading) where the download ends. */
.player-track { position: absolute; left: 0; right: 0; top: 50%; height: 5px; margin-top: -2.5px; border-radius: 3px; background: rgba(255, 255, 255, 0.22); overflow: visible; pointer-events: none; }
.player-downloaded { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 3px; background: rgba(255, 255, 255, 0.34); }
.player-buffered { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 3px; background: rgba(255, 255, 255, 0.42); }
.player-limit { position: absolute; top: -4px; bottom: -4px; width: 3px; margin-left: -1.5px; border-radius: 2px; background: var(--warn, #f5b942); }
.player-scrub { position: absolute; inset: 0; width: 100%; height: 100%; }
.player-scrub::-webkit-slider-runnable-track {
  height: 5px;
  border-radius: 3px;
  background: linear-gradient(to right, var(--accent) var(--progress, 0%), transparent var(--progress, 0%));
}
.player-scrub::-moz-range-track { height: 5px; border-radius: 3px; background: transparent; }
.player-scrub::-moz-range-progress { height: 5px; border-radius: 3px; background: var(--accent); }
.player-scrub::-webkit-slider-thumb { -webkit-appearance: none; width: 14px; height: 14px; margin-top: -4.5px; border-radius: 50%; background: #fff; border: 0; }
.player-scrub::-moz-range-thumb { width: 14px; height: 14px; border-radius: 50%; background: #fff; border: 0; }
.player-scrub:focus-visible, .player-volume-slider:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 4px; }

.player-row { display: flex; align-items: center; gap: var(--space-1); }
.player-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  min-width: 44px;
  height: 44px;
  padding: 0 var(--space-2);
  color: var(--text);
  font: inherit;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius);
  cursor: pointer;
}
.player-btn svg { width: 24px; height: 24px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.player-btn svg.solid { fill: currentColor; stroke: none; }
.player-skip { width: 28px; height: 28px; }
.player-skip .arrow { fill: currentColor; stroke: none; }
.player-skip text { fill: currentColor; stroke: none; font: 700 20px system-ui, sans-serif; }
.player-btn:hover { background: rgba(255, 255, 255, 0.12); }
.player-btn:focus-visible { border-color: var(--focus); outline: none; box-shadow: 0 0 0 3px var(--focus-ring); }
.caption-lang { font-size: 0.8rem; font-weight: 700; text-transform: uppercase; }
.player-btn.caption-btn.on { color: var(--focus); }
.player-volume { display: flex; align-items: center; }
.player-volume-slider { width: 84px; height: 28px; }
.player-volume-slider::-webkit-slider-runnable-track { height: 4px; border-radius: 2px; background: rgba(255, 255, 255, 0.35); }
.player-volume-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 12px; height: 12px; margin-top: -4px; border-radius: 50%; background: #fff; }
.player-volume-slider::-moz-range-track { height: 4px; border-radius: 2px; background: rgba(255, 255, 255, 0.35); }
.player-volume-slider::-moz-range-thumb { width: 12px; height: 12px; border: 0; border-radius: 50%; background: #fff; }
.player-title { margin: 0; font-size: inherit; line-height: inherit; flex: 1 1 0; min-width: 0; padding: 0 var(--space-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; text-align: center; }

.player-pop { position: relative; flex: 0 0 auto; }
.player-menu {
  position: absolute;
  right: 0;
  bottom: calc(100% + var(--space-3));
  z-index: 3;
  display: flex;
  flex-direction: column;
  min-width: 190px;
  max-width: min(360px, 90vw);
  max-height: min(60vh, 420px);
  padding: var(--space-2);
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
}
.player-menu > button {
  padding: var(--space-2) var(--space-3);
  color: var(--text);
  font: inherit;
  text-align: left;
  background: none;
  border: 1px solid transparent;
  border-radius: var(--radius);
  cursor: pointer;
}
.player-menu > button:hover, .player-menu > button:focus-visible { background: var(--surface-2); outline: none; border-color: var(--focus); }
.player-menu > button.selected { color: var(--focus); font-weight: 600; }
.player-menu-label { margin: var(--space-2) var(--space-3) var(--space-1); font-size: 0.75rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-dim); }
.player-menu-empty { margin: var(--space-2) var(--space-3); color: var(--text-dim); font-size: 0.9rem; }
.player-episodes { width: min(380px, 90vw); }
.player-episode-show { margin: var(--space-1) var(--space-3) var(--space-2); font-weight: 700; }
.player-episode-scroll { overflow-y: auto; }
.player-episode-season { margin: var(--space-3) var(--space-3) var(--space-1); font-size: 0.75rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-dim); }
.player-episode {
  display: block;
  width: 100%;
  padding: var(--space-2) var(--space-3);
  color: var(--text);
  font: inherit;
  text-align: left;
  background: none;
  border: 1px solid transparent;
  border-radius: var(--radius);
  cursor: pointer;
}
.player-episode:hover, .player-episode:focus-visible, .player-episode.focused { background: var(--surface-2); outline: none; }
.player-episode:focus-visible { border-color: var(--focus); }
.player-episode.current .player-episode-name { color: var(--focus); font-weight: 600; }
.player-episode-line { display: flex; align-items: baseline; gap: var(--space-3); }
.player-episode-num { min-width: 1.6em; color: var(--text-dim); font-variant-numeric: tabular-nums; }
.player-episode-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.player-episode-now { font-size: 0.75rem; color: var(--focus); }
.player-episode-detail { display: grid; gap: var(--space-2); margin-top: var(--space-2); }
.player-episode-still { aspect-ratio: 16 / 9; background: var(--surface-3) center / cover; border-radius: var(--radius); }
.player-episode-plot { font-size: 0.85rem; line-height: 1.35; color: var(--text-dim); display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }

@media (max-width: 640px) {
  .player-top { padding: var(--space-3) var(--gutter) var(--space-5); }
  .player-controls { padding-top: var(--space-5); }
  .player-row { flex-wrap: wrap; row-gap: 0; }
  .player-title { order: -1; flex: 1 0 100%; padding: 0 0 var(--space-1); text-align: left; }
  .player-volume-slider { display: none; }
  .player-row { gap: 0; }
  .player-btn { min-width: 36px; height: 42px; padding: 0 2px; }
  .player-pop:last-of-type .player-menu, .player-pop .player-menu { right: -8px; }
  .player-scrub-wrap { height: 36px; }
}
</style>
