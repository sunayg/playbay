<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { formatBytes, formatDuration, downloadState, streamState, streamSummary } from "../lib/activity";
import { get, post } from "../lib/api";
import type { Activity, ActivityDownload } from "../types";

const POLL_MS = 3000;

const data = ref<Activity | null>(null);
const error = ref("");
// The delete button asks once more before it deletes: the key of the file it is waiting on.
const confirming = ref("");
let timer: ReturnType<typeof setTimeout> | undefined;
let confirmTimer: ReturnType<typeof setTimeout> | undefined;
let alive = true;

const key = (item: ActivityDownload) => `${item.hash}/${item.filename}`;
const disk = computed(() => {
  const { free, total } = data.value?.disk ?? {};
  return free !== undefined && total ? `${formatBytes(free)} free of ${formatBytes(total)}` : "";
});
const downloading = computed(() => (data.value?.downloads ?? []).filter((d) => d.status !== "ready"));
const kept = computed(() => (data.value?.downloads ?? []).filter((d) => d.status === "ready"));

async function load(): Promise<void> {
  try {
    data.value = await get<Activity>("/api/activity");
    error.value = "";
  } catch (e) {
    if (alive) error.value = (e as Error).message;
  }
  if (alive) timer = setTimeout(load, POLL_MS);
}

async function act(url: string): Promise<void> {
  try {
    await post(url);
  } catch (e) {
    error.value = (e as Error).message;
  }
  clearTimeout(timer);
  void load();
}

const endStream = (id: string) => act(`/api/activity/streams/${encodeURIComponent(id)}/end`);

function remove(item: ActivityDownload): void {
  // Stopping a download in progress is not a loss; deleting a kept file is.
  if (item.status !== "ready" || confirming.value === key(item)) {
    confirming.value = "";
    void act(`/api/activity/downloads/${item.hash}/${encodeURIComponent(item.filename)}/remove`);
    return;
  }
  confirming.value = key(item);
  clearTimeout(confirmTimer);
  confirmTimer = setTimeout(() => (confirming.value = ""), 4000);
}

onMounted(load);
onBeforeUnmount(() => {
  alive = false;
  clearTimeout(timer);
  clearTimeout(confirmTimer);
});
</script>

<template>
  <header class="top">
    <RouterLink to="/" class="back">&larr; PlayBay</RouterLink>
    <h1>Activity</h1>
  </header>
  <main class="activity" data-screen="activity-screen">
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="!data && !error" class="loading">Loading…</p>
    <template v-if="data">
      <section aria-labelledby="streams-title">
        <h2 id="streams-title">Streams</h2>
        <p v-if="!data.streams.length" class="activity-empty">Nothing is being watched.</p>
        <ul class="activity-list">
          <li v-for="s in data.streams" :key="s.id" class="activity-item" data-kind="stream">
            <div class="activity-main">
              <div class="activity-name" :title="s.filename">{{ s.filename }}</div>
              <div class="activity-meta">{{ streamSummary(s) }}</div>
              <div class="activity-meta" :class="{ warn: s.failed || (s.speed !== null && s.speed < 1) }">{{ streamState(s) }}</div>
              <div
                class="activity-bar"
                role="progressbar"
                :aria-valuenow="Math.round(s.position_seconds)"
                aria-valuemin="0"
                :aria-valuemax="Math.round(s.duration)"
                aria-label="Playback position"
              >
                <div class="activity-bar-encoded" :style="{ width: Math.min(100, (s.encoded_seconds / s.duration) * 100) + '%' }"></div>
                <div class="activity-bar-position" :style="{ left: Math.min(100, (s.position_seconds / s.duration) * 100) + '%' }"></div>
              </div>
              <div class="activity-meta">
                At {{ formatDuration(s.position_seconds) }} of {{ formatDuration(s.duration) }}, encoded to {{ formatDuration(s.encoded_seconds) }}
                <span v-if="s.restarts"> · restarted {{ s.restarts }}×</span>
              </div>
            </div>
            <button type="button" class="activity-button" @click="endStream(s.id)">End stream</button>
          </li>
        </ul>
      </section>

      <section aria-labelledby="downloads-title">
        <h2 id="downloads-title">Downloads</h2>
        <p v-if="disk" class="activity-meta">Disk: {{ disk }}</p>
        <p v-if="!downloading.length" class="activity-empty">No downloads in progress.</p>
        <ul class="activity-list">
          <li v-for="d in downloading" :key="key(d)" class="activity-item" data-kind="download">
            <div class="activity-main">
              <div class="activity-name" :title="d.filename">{{ d.filename }}</div>
              <div class="activity-meta">{{ downloadState(d) }}</div>
              <div v-if="d.progress !== null" class="activity-bar" role="progressbar" :aria-valuenow="Math.round(d.progress * 100)" aria-valuemin="0" aria-valuemax="100" aria-label="Download progress">
                <div class="activity-bar-encoded" :style="{ width: Math.round(d.progress * 100) + '%' }"></div>
              </div>
            </div>
            <button type="button" class="activity-button" @click="remove(d)">Cancel</button>
          </li>
        </ul>
      </section>

      <section aria-labelledby="kept-title">
        <h2 id="kept-title">Kept files</h2>
        <p v-if="!kept.length" class="activity-empty">No files are kept.</p>
        <ul class="activity-list">
          <li v-for="d in kept" :key="key(d)" class="activity-item" data-kind="kept">
            <div class="activity-main">
              <div class="activity-name" :title="d.filename">{{ d.filename }}</div>
              <div class="activity-meta">{{ downloadState(d) }}</div>
            </div>
            <button type="button" class="activity-button danger" @click="remove(d)">
              {{ confirming === key(d) ? "Delete for good?" : "Delete" }}
            </button>
          </li>
        </ul>
      </section>
    </template>
  </main>
</template>

<style>
.activity { max-width: 56rem; margin: 0 auto; padding: var(--space-4); display: grid; gap: var(--space-5); }
.activity h2 { margin: 0 0 var(--space-2); font-size: 1.1rem; }
.activity-list { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-3); }
.activity-item { display: flex; gap: var(--space-4); align-items: center; justify-content: space-between; padding: var(--space-3); border: 1px solid var(--border); border-radius: 8px; }
.activity-main { min-width: 0; flex: 1; display: grid; gap: var(--space-1); }
.activity-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.activity-meta { color: var(--text-dim); font-size: 0.9rem; overflow-wrap: anywhere; }
.activity-meta.warn { color: var(--warn, #f5b942); }
.activity-empty { color: var(--text-dim); margin: 0; }
.activity-bar { position: relative; height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; }
.activity-bar-encoded { position: absolute; inset: 0 auto 0 0; background: var(--text-dim); }
.activity-bar-position { position: absolute; top: 0; bottom: 0; width: 3px; margin-left: -1.5px; background: var(--accent, #4ade80); }
.activity-button { flex: 0 0 auto; padding: var(--space-2) var(--space-3); border: 1px solid var(--border); border-radius: 6px; background: transparent; color: var(--text); cursor: pointer; }
.activity-button:hover, .activity-button:focus-visible { border-color: var(--text); }
.activity-button.danger { color: var(--warn, #f5b942); }
@media (max-width: 40rem) { .activity-item { flex-direction: column; align-items: stretch; } }
</style>
