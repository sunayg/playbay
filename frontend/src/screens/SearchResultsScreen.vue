<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import GroupCard from "../components/search/GroupCard.vue";
import ResultList from "../components/search/ResultList.vue";
import LoadingSpinner from "../components/LoadingSpinner.vue";
import { get, isAbortError, postJSON, restoreCookie, routeTimeout, subscribe } from "../lib/api";
import { isTextEntry } from "../lib/focus";
import { fetchWatchRows, getFavorites, getHistory, readLocalTitles, saveSearchTerm, syncLibraryFromLocalHistory } from "../lib/library";
import { get_hash, magnetPath, pinnedTitleFromLocation, rememberEpisodeTarget, searchPath, torrentPath, type PinnedTitle } from "../lib/magnet";
import {
  flatRows,
  groupKey,
  libraryMatch,
  type Group,
  type ResultRow,
  type RichPayload,
  type SearchResult,
} from "../lib/results";

// Served by the backend from public/lib; kept out of the bundler on purpose.
const LOGO = "/lib/playbay-logo.svg";

const route = useRoute();
const router = useRouter();

const searchterm = ref("");
const query = ref("");
const pinned = ref<PinnedTitle | null>(null);
// null while loading; a list (possibly empty) once something can be shown.
const results = shallowRef<SearchResult[] | null>(null);
const groups = shallowRef<Group[]>([]);
const other = shallowRef<SearchResult[]>([]);
const pending = ref(false);
const failed = ref(false);
const autoOpenKey = ref<string | null>(null);
const libraryTitles = ref(readLocalTitles());

let generation = 0;
let settled = false;
let alive = true;
let source: EventSource | null = null;
let flatSource: EventSource | null = null;

const isHistory = computed(() => searchterm.value === "[history]");
const isFavorites = computed(() => searchterm.value === "[favorites]");

// Rich search keeps ungrouped hits in `other` and parks `results` as an empty
// list; history, favorites, community searches and the plain-search fallback
// fill `results` and leave `other` empty.
const flat = computed(() => (results.value && results.value.length ? results.value : other.value));
const heading = computed(() => {
  if (pinned.value) return "";
  if (isHistory.value) return "History";
  if (isFavorites.value) return "Favorites";
  return searchterm.value ? "" : "Latest releases";
});
const emptyMessage = computed(() => {
  if (isHistory.value) return "Nothing in your history yet.";
  if (isFavorites.value) return "No favorites yet. Tap the star on a file to add one.";
  return searchterm.value ? `No results for “${searchterm.value}”.` : "No results right now.";
});

// ---- actions ---------------------------------------------------------------------

function onSubmit(): void {
  const term = (query.value || "").trim();
  if (!term) return;
  if (term.startsWith("magnet:")) {
    void router.push(magnetPath(term));
    return;
  }
  saveSearchTerm(term);
  void router.push(searchPath(term));
}

function openResult(result: SearchResult, season: number | null = null, episode: number | null = null): void {
  rememberEpisodeTarget(season, episode);
  if (result.filename && result.magnet) void router.push(magnetPath(result.magnet, result.filename));
  else if (result.magnet) void router.push(magnetPath(result.magnet));
  else if (result.torrent_link) void router.push(torrentPath(result.torrent_link));
}

function onFlatRow(row: ResultRow): void {
  if (row.type === "result") openResult(row.result);
}

// ---- focus -------------------------------------------------------------------------

const FIRST_RESULT =
  ".group-card-collapsed, .group-card .episode-card, .group-card .season-toggle, .group-card .result-row, .results-body .result-row";

function focusFirstResult(): void {
  routeTimeout(() => {
    if (!alive) return;
    // Do not pull the focus away from a search being edited.
    if (isTextEntry(document.activeElement) && query.value.trim() !== searchterm.value) return;
    document.querySelector<HTMLElement>(FIRST_RESULT)?.focus();
  }, 0);
}

// ---- library marks -------------------------------------------------------------------

async function loadLibrary(): Promise<void> {
  libraryTitles.value = readLocalTitles();
  void syncLibraryFromLocalHistory();
  await fetchWatchRows();
  if (alive) libraryTitles.value = readLocalTitles();
}

const matchFor = (group: Group) => libraryMatch(libraryTitles.value, group);

// ---- history and favorites ------------------------------------------------------------

async function applyStatuses(list: SearchResult[], gen: number): Promise<void> {
  const hashes: string[] = [];
  const torrentLinks: string[] = [];
  for (const item of list) {
    if (item.magnet) hashes.push(get_hash(item.magnet));
    else if (item.torrent_link) torrentLinks.push(item.torrent_link);
  }
  if (!hashes.length && !torrentLinks.length) return;
  try {
    const data = await postJSON<{ statuses?: Record<string, string | null> }>("/api/magnet_status/", {
      hashes,
      torrent_links: torrentLinks,
    });
    if (gen !== generation) return;
    const statuses = data.statuses || {};
    results.value = list.map((item) => ({
      ...item,
      status: item.magnet ? statuses[get_hash(item.magnet)] || null : item.torrent_link ? statuses[item.torrent_link] || null : null,
    }));
  } catch {
    // Status marks are decoration; the list works without them.
  }
}

function showSaved(list: SearchResult[], gen: number): void {
  results.value = list;
  focusFirstResult();
  void applyStatuses(list, gen);
}

// ---- plain search (SSE with a request fallback) ----------------------------------------

function startFlatSearch(gen: number): void {
  results.value = null;
  groups.value = [];
  other.value = [];
  pending.value = true;
  const term = encodeURIComponent(searchterm.value);
  const fallback = () => {
    flatSource = null;
    if (gen !== generation || results.value !== null) {
      pending.value = false;
      return;
    }
    get<{ results?: SearchResult[] }>("/api/search/" + term).then(
      (data) => {
        if (gen !== generation) return;
        pending.value = false;
        results.value = data.results || [];
        focusFirstResult();
      },
      (error) => {
        if (isAbortError(error) || gen !== generation) return;
        pending.value = false;
        failed.value = true;
      },
    );
  };
  let focused = false;
  // Each indexer's answer arrives as a re-ranked snapshot of everything so far.
  flatSource = subscribe<{ results?: SearchResult[] | null; done?: boolean }>(
    "/api/search_events/" + term,
    (data) => {
      if (gen !== generation) return;
      if (data.results) {
        results.value = data.results;
        if (!focused && data.results.length) {
          focused = true;
          focusFirstResult();
        }
      }
      if (data.done) {
        // Settle an empty stream so the spinner stops.
        if (results.value === null) {
          results.value = data.results || [];
          focusFirstResult();
        }
        pending.value = false;
        flatSource = null;
      }
    },
    fallback,
  );
}

// ---- rich search ----------------------------------------------------------------------------

function resetRich(): void {
  results.value = null;
  groups.value = [];
  other.value = [];
  pending.value = false;
  failed.value = false;
  autoOpenKey.value = null;
  settled = false;
}

function expandPinnedGroup(): void {
  const wanted = pinned.value;
  if (!wanted || !groups.value.length) return;
  const match =
    groups.value.find((g) => String(g.tmdb_id) === String(wanted.tmdb) && g.media_type === wanted.media) ||
    (groups.value.length === 1 ? groups.value[0] : null);
  if (match) autoOpenKey.value = groupKey(match);
}

function applyRich(data: RichPayload, gen: number): void {
  if (gen !== generation) return;
  groups.value = Array.isArray(data.groups) ? data.groups : [];
  other.value = Array.isArray(data.other) ? data.other : [];
  expandPinnedGroup();
  const visible = groups.value.length > 0 || other.value.length > 0;
  if (visible && results.value === null) {
    results.value = [];
    focusFirstResult();
  }
  if (!data.done) return;
  pending.value = false;
  settled = true;
  if (!pinned.value && groups.value.length === 1) autoOpenKey.value = groupKey(groups.value[0]);
  if (results.value !== null) return;
  if (pinned.value || visible) {
    results.value = [];
    focusFirstResult();
    return;
  }
  startFlatSearch(gen);
}

function loadBlockingRich(gen: number): void {
  resetRich();
  let endpoint = "/api/rich_search/" + encodeURIComponent(searchterm.value);
  if (pinned.value) {
    endpoint += "?media=" + encodeURIComponent(pinned.value.media) + "&tmdb=" + encodeURIComponent(pinned.value.tmdb);
  }
  get<RichPayload>(endpoint).then(
    (data) => applyRich({ ...data, done: true }, gen),
    (error) => {
      if (isAbortError(error) || gen !== generation) return;
      pending.value = false;
      if (pinned.value) {
        groups.value = [];
        other.value = [];
        results.value = [];
        return;
      }
      if (results.value === null) startFlatSearch(gen);
    },
  );
}

function startRichSearch(gen: number): void {
  resetRich();
  pending.value = true;
  if (typeof window.EventSource === "undefined") {
    loadBlockingRich(gen);
    return;
  }
  restoreCookie();
  const stream = new EventSource("/api/rich_search_events/" + encodeURIComponent(searchterm.value));
  source = stream;
  stream.onmessage = (event) => {
    if (gen !== generation) {
      stream.close();
      return;
    }
    let data: RichPayload;
    try {
      data = JSON.parse(event.data) as RichPayload;
    } catch {
      return;
    }
    applyRich(data, gen);
    if (data.done) {
      settled = true;
      stream.close();
      if (source === stream) source = null;
    }
  };
  stream.onerror = () => {
    stream.close();
    if (source === stream) source = null;
    if (gen !== generation || settled || results.value !== null) {
      pending.value = false;
      settled = true;
      return;
    }
    loadBlockingRich(gen);
  };
}

// ---- lifecycle -----------------------------------------------------------------------------------

function closeStreams(): void {
  source?.close();
  source = null;
  flatSource?.close();
  flatSource = null;
}

function routeKey(): string {
  return [route.params.searchterm, route.query.media, route.query.tmdb].join("|");
}

function load(): void {
  generation += 1;
  const gen = generation;
  closeStreams();
  const raw = route.params.searchterm;
  searchterm.value = (Array.isArray(raw) ? raw.join("/") : raw) || "";
  query.value = searchterm.value === "[history]" || searchterm.value === "[favorites]" ? "" : searchterm.value;
  const search = new URLSearchParams();
  for (const name of ["media", "tmdb"]) {
    const value = route.query[name];
    if (typeof value === "string") search.set(name, value);
  }
  pinned.value = pinnedTitleFromLocation("?" + search.toString());
  resetRich();

  if (isHistory.value) {
    showSaved(getHistory() as SearchResult[], gen);
  } else if (isFavorites.value) {
    showSaved(getFavorites() as SearchResult[], gen);
  } else if (pinned.value) {
    loadBlockingRich(gen);
  } else {
    startRichSearch(gen);
  }
}

onMounted(() => {
  load();
  void loadLibrary();
});

// The same screen serves every term: a new term or pinned title reloads it.
watch(routeKey, () => {
  if (route.name === "search") load();
});

onBeforeUnmount(() => {
  alive = false;
  generation += 1;
  closeStreams();
});
</script>

<template>
  <div class="results-root" data-screen="search-results-screen">
    <header class="topbar">
      <RouterLink class="home-logo-link" to="/" aria-label="PlayBay home">
        <img class="home-logo" :src="LOGO" alt="PlayBay" />
      </RouterLink>
      <form v-if="!pinned" class="home-search" role="search" @submit.prevent="onSubmit">
        <svg class="home-search-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.5 15.5L21 21" />
        </svg>
        <input v-model="query" type="text" class="form-control" autocorrect="off" autocapitalize="none" placeholder="Search" aria-label="Search" />
      </form>
    </header>

    <main class="results-body">
      <h1 v-if="heading" class="results-heading">{{ heading }}</h1>
      <LoadingSpinner v-if="results === null && !failed" />
      <div v-else-if="failed" class="results-error" role="alert">
        <p>The search failed. Check the indexer connection and try again.</p>
        <button type="button" class="results-retry" @click="load">Try again</button>
      </div>
      <template v-else>
        <div v-if="groups.length" class="grouped-results">
          <GroupCard
            v-for="group in groups"
            :key="groupKey(group)"
            :group="group"
            :auto-open="autoOpenKey === groupKey(group)"
            :watched-at="matchFor(group)?.watched_at"
            :watched-episodes="matchFor(group)?.watched_episodes"
            @select="openResult"
          />
        </div>
        <section v-if="groups.length && other.length" class="other-results">
          <h2 class="other-results-title">Other results</h2>
          <ResultList :rows="flatRows(other)" label="Other results" @select="onFlatRow" />
        </section>
        <template v-if="!groups.length">
          <p v-if="!pending && !flat.length" class="empty-state">{{ emptyMessage }}</p>
          <ResultList v-if="flat.length" :rows="flatRows(flat)" label="Results" @select="onFlatRow" />
        </template>
        <p v-if="pending" class="rich-search-pending" role="status">Loading more results</p>
      </template>
    </main>
  </div>
</template>

<style>
.results-root { min-height: 100vh; min-height: 100dvh; text-align: left; }
.results-root .topbar { position: sticky; top: 0; }
.results-body { position: relative; max-width: 1040px; min-height: 60vh; margin: 0 auto; padding: var(--space-4) var(--gutter) 64px; }
.results-heading { margin: var(--space-3) 0 var(--space-4); font-size: 1.5rem; }
.grouped-results { display: flex; flex-direction: column; gap: var(--space-4); }
.results-body .empty-state { margin: var(--space-5) 0; }
.other-results { margin-top: var(--space-6); }
.other-results-title { margin: 0 0 var(--space-2); font-size: 1.1rem; }
.rich-search-pending { margin: var(--space-4) 0; color: var(--text-dim); }
.results-error { margin: var(--space-6) 0; color: var(--danger-text); }
.results-retry {
  padding: var(--space-2) var(--space-4);
  color: var(--text);
  font: inherit;
  background: var(--surface-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  cursor: pointer;
}
.results-retry:focus-visible, .results-retry:hover { border-color: var(--focus); outline: none; box-shadow: 0 0 0 3px var(--focus-ring); }
.results-body > .result-list { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
.other-results .result-list { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
</style>
