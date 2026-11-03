<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import BackButton from "../components/BackButton.vue";
import FavoriteButton from "../components/FavoriteButton.vue";
import FullscreenButton from "../components/FullscreenButton.vue";
import LoadingSpinner from "../components/LoadingSpinner.vue";
import PlayerHost from "../components/PlayerHost.vue";
import { get, isAbortError, post, routeTimeout, subscribe } from "../lib/api";
import {
  canCancelStatus,
  headingFor,
  isAppleBrowser,
  isHlsLink,
  mp4Link,
  playLinkIdentity,
  streamDeclinedMessage,
  subheadingFor,
  subtitleTracks,
  type FileInfo,
  type SubtitleTrack,
} from "../lib/download";
import { getVideoPosition, reportLibraryEvents, saveToHistory } from "../lib/library";
import { navigateBack } from "../lib/nav";
import { decodeMagnetParam, get_hash, get_magnet_name, magnetPath } from "../lib/magnet";
import { viewerId } from "../lib/viewer";

const route = useRoute();
const router = useRouter();

const magnet = computed(() => decodeMagnetParam(String(route.params.magnet_link ?? "")));
const filename = computed(() => String(route.params.filename ?? ""));
const hash = computed(() => get_hash(magnet.value));
const backTarget = computed(() => magnetPath(magnet.value));

const status = ref<string | null>(null);
const progress = ref<number | null>(null); // percent, for the bar
const heading = ref("");
const subheading = ref<string | null>(null);
const playLink = ref<string | null>(null);
const subtitles = ref<SubtitleTrack[]>([]);
const supported = ref<boolean | null>(null);
const downloadProgress = ref<number | null>(null);
const startAt = ref<number | null>(null); // percent of the file where playback can begin
const hlsFailed = ref(false);
const streamMessage = ref<string | null>(null);
const cancelling = ref(false);
const failed = ref<string | null>(null);

const canCancel = computed(() => canCancelStatus(status.value));

let alive = true;
let source: EventSource | null = null;
let polling = false;

function closeSource(): void {
  if (source) source.close();
  source = null;
}

function goBack(): void {
  navigateBack(router, backTarget.value);
}

// ---- status -> view state (legacy applyFileInfo) ---------------------------------

function applyFileInfo(data: FileInfo): void {
  if (!alive || !data || !data.status) return;
  failed.value = null;
  status.value = data.status;
  progress.value = data.progress === 0 || data.progress ? data.progress * 100 : null;
  heading.value = headingFor(data);
  subheading.value = subheadingFor(data);

  // An MP4 the browser can play as it is plays directly. Anything else plays through an encode
  // session, which also works while the file is still downloading.
  if (data.status === "ready" && data.filename && data.playback === "direct") {
    const link = mp4Link(hash.value, data.filename);
    if (playLink.value !== link) playLink.value = link;
    supported.value = !!data.supported;
  } else if (!playLink.value && !hlsFailed.value && (data.can_stream || data.status === "ready")) {
    void openSession(data.status === "ready");
  }
  downloadProgress.value = data.status !== "ready" ? data.progress || null : null;
  startAt.value = typeof data.start_at === "number" ? data.start_at * 100 : null;

  // <track> subtitles are for a directly played MP4; an encode session declares them in its playlist.
  const playingSession = isHlsLink(playLink.value);
  if (!isAppleBrowser() && !playingSession) subtitles.value = subtitleTracks(hash.value, data.subtitles || []);
  else if (playingSession) subtitles.value = [];
}

function poll(): void {
  if (polling) return;
  polling = true;
  const tick = async (): Promise<void> => {
    try {
      const data = await get<FileInfo>("/api/magnet/" + hash.value + "/" + encodeURIComponent(filename.value));
      if (!alive) return;
      applyFileInfo(data);
      if (status.value !== "ready") routeTimeout(tick, 1000);
    } catch (error) {
      if (alive && !isAbortError(error)) failed.value = "Lost contact with the server.";
    }
  };
  void tick();
}

// ---- actions -----------------------------------------------------------------------

let sessionRequested = false;

// Asks the server for an encode session, resuming where the viewer left off. While the file
// is still downloading, a refusal is only a message (and a later try); once it is ready, it is an error.
async function openSession(ready: boolean): Promise<void> {
  if (sessionRequested) return;
  sessionRequested = true;
  streamMessage.value = null;
  // The viewer may move to another file while this request is out: its answer belongs to the old one.
  const file = magnet.value + "\n" + filename.value;
  const stale = () => !alive || file !== magnet.value + "\n" + filename.value;
  try {
    const start = getVideoPosition(magnet.value, filename.value);
    const data = await post<{ url?: string | null; reason?: string }>(
      "/api/magnet/" + hash.value + "/" + encodeURIComponent(filename.value) + "/session?start=" + Math.floor(start) + "&viewer=" + viewerId(),
    );
    if (stale()) return;
    if (data && data.url) {
      playLink.value = data.url;
      supported.value = true;
      return;
    }
    const reason = data && data.reason;
    if (ready && reason !== "capacity") {
      sessionRequested = false;
      failed.value = streamDeclinedMessage(reason);
    } else {
      streamMessage.value = streamDeclinedMessage(reason);
      // Not ready or at capacity (other streams can free up, ready file or not): try again in a moment.
      // Other reasons will not change. A plain timer:
      // routeTimeout would cancel the status polling timer, which also keeps one pending.
      if (reason === "not_ready" || reason === "capacity") setTimeout(() => !stale() && (sessionRequested = false), 5000);
    }
  } catch {
    if (stale()) return;
    sessionRequested = false;
    failed.value = "Lost contact with the server.";
  }
}

async function cancelDownload(): Promise<void> {
  if (cancelling.value) return;
  cancelling.value = true;
  try {
    await post("/api/magnet/" + hash.value + "/" + encodeURIComponent(filename.value) + "/cancel");
  } catch {
    // Leave anyway; the server drops the download when nobody watches it.
  }
  if (alive) goBack();
}

// Playback failed. A session closes on the server after sitting idle (a long pause), so the first failure of a
// session link opens a new one where the viewer was; a second one soon after means a real problem (an
// unsupported codec, a broken file): drop back to the progress view.
let lastReopen = 0;
function onStreamError(): void {
  const failedLink = playLink.value;
  playLink.value = null;
  if (failedLink && failedLink.startsWith("/api/hls/") && Date.now() - lastReopen > 30_000) {
    lastReopen = Date.now();
    sessionRequested = false;
    void openSession(status.value === "ready");
    return;
  }
  hlsFailed.value = true;
  // A ready file has no progress view to fall back to: say so, with the retry button.
  if (status.value === "ready") failed.value = "Playback failed. Try again.";
}

async function start(): Promise<void> {
  failed.value = null;
  try {
    await post("/api/magnet_download/", { magnet_link: magnet.value, filename: filename.value });
  } catch (error) {
    if (alive && !isAbortError(error)) failed.value = "Couldn't start the download.";
    return;
  }
  if (!alive) return;
  saveToHistory(magnet.value);
  void reportLibraryEvents([
    { event: "download", magnet: magnet.value, title: get_magnet_name(magnet.value), filename: filename.value, ts: Date.now() },
  ]);
  // The server pushes progress, peers and ready as they happen.
  source = subscribe<FileInfo>(
    "/api/magnet_events/" + hash.value + "/" + encodeURIComponent(filename.value),
    (data) => {
      applyFileInfo(data);
      if (data.done) source = null;
    },
    () => {
      source = null;
      if (status.value !== "ready") poll();
    },
  );
}

function retry(): void {
  hlsFailed.value = false;
  sessionRequested = false;
  lastReopen = 0;
  polling = false;
  closeSource();
  void start();
}

onMounted(start);
onBeforeUnmount(() => {
  alive = false;
  closeSource();
});
watch([magnet, filename], () => {
  // Same screen, different file: start over.
  closeSource();
  polling = false;
  status.value = null;
  progress.value = null;
  startAt.value = null;
  playLink.value = null;
  subtitles.value = [];
  hlsFailed.value = false;
  sessionRequested = false;
  lastReopen = 0;
  streamMessage.value = null;
  cancelling.value = false;
  heading.value = "";
  subheading.value = null;
  void start();
});
</script>

<template>
  <PlayerHost
    v-if="playLink"
    :key="playLinkIdentity(playLink)"
    :url="playLink"
    :supported="supported"
    :subtitles="subtitles"
    :magnet="magnet"
    :filename="filename"
    :download-progress="downloadProgress"
    :download-status="status"
    @back="goBack"
    @stream-error="onStreamError"
  />
  <main v-else class="dl-root" data-screen="download-screen">
    <div class="topbar dl-topbar">
      <BackButton :fallback="backTarget" />
      <h1 class="dl-title" :title="filename">{{ filename }}</h1>
      <div class="dl-actions">
        <FavoriteButton :magnet="magnet" :filename="filename" />
        <FullscreenButton />
      </div>
    </div>
    <div class="dl-body dl-status">
      <div v-if="failed" class="dl-error" role="alert">
        <p>{{ failed }}</p>
        <button type="button" class="dl-button" @click="retry">Try again</button>
      </div>
      <template v-else>
        <LoadingSpinner
          v-if="status !== 'ready'"
          :heading="heading || 'Starting'"
          :subheading="subheading ?? ''"
          :progress="progress"
          :marker="status === 'downloading' ? startAt : null"
          marker-label="Playback starts here"
        />
        <p v-if="streamMessage" class="stream-message" role="status">{{ streamMessage }}</p>
        <button v-if="canCancel" type="button" class="dl-button cancel-download" :disabled="cancelling" @click="cancelDownload">
          Cancel download
        </button>
      </template>
    </div>
  </main>
</template>

<style>
.stream-btn svg { fill: currentColor; stroke: none; }
.dl-status { display: flex; flex-direction: column; align-items: center; justify-content: flex-end; gap: var(--space-4); }
.stream-message { margin: 0; color: var(--text-dim); text-align: center; }
.cancel-download { position: relative; margin-top: calc(var(--space-6) * 4); }
</style>
