<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import BillboardCard from "../components/BillboardCard.vue";
import HomeRail from "../components/HomeRail.vue";
import LoadingSpinner from "../components/LoadingSpinner.vue";
import PosterCard from "../components/PosterCard.vue";
import { isAbortError, routeTimeout } from "../lib/api";
import {
  clearSearchHistory,
  fetchHome,
  fetchWatchRows,
  library,
  refreshLibrary,
  saveSearchTerm,
  syncLibraryFromLocalHistory,
} from "../lib/library";
import { magnetPath, searchPath } from "../lib/magnet";
import { titleSearchQuery, type HomeTitle } from "../lib/titles";

// Served by the backend from public/lib; kept out of the bundler on purpose.
const LOGO = "/lib/playbay-logo.svg";

const router = useRouter();
const route = useRoute();

const searchterm = ref("");
const searchInput = ref<HTMLInputElement | null>(null);
const keepWatching = ref<HomeTitle[]>([]);
const recentTitles = ref<HomeTitle[]>([]);
const topSeries = ref<HomeTitle[]>([]);
const topMovies = ref<HomeTitle[]>([]);
const homeLoaded = ref(false);
let homeRetried = false;
let alive = true;

const isEmpty = computed(
  () =>
    homeLoaded.value &&
    !keepWatching.value.length &&
    !recentTitles.value.length &&
    !topSeries.value.length &&
    !topMovies.value.length &&
    !library.searchHistory.length,
);

async function loadLocalRows(): Promise<void> {
  const data = await fetchWatchRows();
  if (!alive) return;
  homeLoaded.value = true;
  keepWatching.value = data.keep_watching as HomeTitle[];
  recentTitles.value = data.recent as HomeTitle[];
  const missingArtwork = [...recentTitles.value, ...topSeries.value, ...topMovies.value].some((item) => item && !item.backdrop_url);
  // Artwork is fetched lazily by the server, so look once more shortly after.
  if (missingArtwork && !homeRetried) {
    homeRetried = true;
    routeTimeout(() => void loadLocalRows(), 2500);
  }
}

async function loadHome(): Promise<void> {
  try {
    const data = await fetchHome();
    if (!alive) return;
    topSeries.value = data.series as HomeTitle[];
    topMovies.value = data.movies as HomeTitle[];
  } catch (error) {
    if (isAbortError(error) || !alive) return;
  }
  await loadLocalRows();
}

function resumeTitle(title: HomeTitle): void {
  if (title.magnet && title.filename) {
    void router.push(magnetPath(title.magnet, title.filename));
    return;
  }
  openTitle(title);
}

function openTitle(title: HomeTitle): void {
  const term = title.title || "";
  if (!term) return;
  searchterm.value = term;
  saveSearchTerm(term);
  void router.push(searchPath(term) + titleSearchQuery(title));
}

function onSubmit(): void {
  const term = searchterm.value;
  if (term.startsWith("magnet:")) {
    void router.push(magnetPath(term));
    return;
  }
  saveSearchTerm(term);
  void router.push(searchPath(term));
}

function onHistoryClick(term: string): void {
  searchterm.value = term;
  saveSearchTerm(term);
  void router.push(searchPath(term));
}

const goHistory = () => void router.push("/search/[history]");
const goTrending = () => void router.push("/search/");
const goFavorites = () => void router.push("/search/[favorites]");
const goSettings = () => void router.push("/settings");

onMounted(() => {
  refreshLibrary();
  void syncLibraryFromLocalHistory();
  void loadHome();

  if (route.path.toLowerCase() === "/registerhandler") {
    try {
      navigator.registerProtocolHandler("magnet", window.location.origin + (router.options.history.base || "") + "/magnet/%s");
    } catch {
      // not supported or not allowed here
    }
  }

  // Not on a TV or a phone: there the on-screen keyboard would cover the rows.
  const coarse = typeof window.matchMedia === "function" && window.matchMedia("(pointer: coarse)").matches;
  if (!coarse && !document.documentElement.classList.contains("tv-ui")) searchInput.value?.focus();
});

onBeforeUnmount(() => {
  alive = false;
});
</script>

<template>
  <div class="home-root" data-screen="search-screen">
    <header class="topbar">
      <RouterLink class="home-logo-link" to="/" aria-label="PlayBay home">
        <img class="home-logo" :src="LOGO" alt="PlayBay" />
      </RouterLink>
      <form class="home-search" role="search" @submit.prevent="onSubmit">
        <svg class="home-search-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.5 15.5L21 21" />
        </svg>
        <input
          ref="searchInput"
          v-model="searchterm"
          type="text"
          class="form-control"
          autocorrect="off"
          autocapitalize="none"
          placeholder="Search"
          aria-label="Search"
          data-exit-right
        />
      </form>
      <nav class="home-nav" aria-label="Shortcuts">
        <button type="button" class="home-shortcut" @click="goHistory">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7M3 4v5h5M12 7v5l3 2" /></svg>
          <span>History</span>
        </button>
        <button type="button" class="home-shortcut" @click="goTrending">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17l6-6 4 4 8-8M15 7h6v6" /></svg>
          <span>Latest releases</span>
        </button>
        <button type="button" class="home-shortcut" @click="goFavorites">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-7-4.5-9.5-9A5.5 5.5 0 0 1 12 6a5.5 5.5 0 0 1 9.5 6C19 16.5 12 21 12 21z" /></svg>
          <span>Favorites</span>
        </button>
        <button type="button" class="home-shortcut" @click="goSettings">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1" /></svg>
          <span>Settings</span>
        </button>
      </nav>
    </header>
    <main class="home-stage">
      <h1 class="sr-only">PlayBay</h1>
      <div class="home-rows">
        <p v-if="isEmpty" class="empty-state home-empty">Search for a movie or show to get started.</p>
        <LoadingSpinner v-else-if="!homeLoaded" inline />

        <HomeRail v-if="keepWatching.length" heading="Keep watching">
          <BillboardCard
            v-for="(title, index) in keepWatching"
            :key="'keep-' + title.media_type + ':' + title.tmdb_id + ':' + index"
            :title="title"
            show-remaining
            @select="resumeTitle"
          />
        </HomeRail>
        <HomeRail v-if="recentTitles.length" heading="Recently watched">
          <BillboardCard
            v-for="(title, index) in recentTitles"
            :key="'recent-' + title.media_type + ':' + title.tmdb_id + ':' + index"
            :title="title"
            show-next
            @select="openTitle"
          />
        </HomeRail>
        <HomeRail v-if="topSeries.length" heading="Trending series this week" scroller-class="poster-scroller">
          <PosterCard v-for="title in topSeries" :key="'series-' + title.tmdb_id" :title="title" show-next @select="openTitle" />
        </HomeRail>
        <HomeRail v-if="topMovies.length" heading="Trending movies this week" scroller-class="poster-scroller">
          <PosterCard v-for="title in topMovies" :key="'movie-' + title.tmdb_id" :title="title" @select="openTitle" />
        </HomeRail>
        <HomeRail v-if="library.searchHistory.length" heading="Recent searches">
          <button
            v-for="term in library.searchHistory"
            :key="'search-' + term"
            type="button"
            class="search-chip"
            @click="onHistoryClick(term)"
          >
            {{ term }}
          </button>
          <template #footer>
            <button type="button" class="search-history-clear" @click="clearSearchHistory">Clear search history</button>
          </template>
        </HomeRail>
      </div>
    </main>
  </div>
</template>

<style>
.home-root {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  min-height: 0;
  overflow: hidden;
  text-align: left;
}
.home-stage { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.home-rows {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding: var(--space-5) 0 48px;
  scrollbar-width: none;
}
.home-rows::-webkit-scrollbar { display: none; }

.search-chip {
  flex: 0 0 auto;
  padding: 10px var(--space-4);
  color: var(--text);
  font: inherit;
  font-size: 0.95rem;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
}
.search-chip:hover, .search-chip:focus-visible { background: var(--surface-3); border-color: var(--focus); outline: none; box-shadow: 0 0 0 3px var(--focus-ring); }
.search-history-clear {
  display: block;
  width: fit-content;
  margin: var(--space-2) 0 0 var(--gutter);
  padding: 4px var(--space-2);
  color: var(--text-dim);
  font: inherit;
  font-size: 0.875rem;
  background: none;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
}
.search-history-clear:hover, .search-history-clear:focus-visible { color: var(--text); outline: 2px solid var(--focus); outline-offset: 2px; }

.home-nav { display: flex; flex: 0 0 auto; align-items: center; gap: var(--space-2); }
.home-shortcut {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 8px 10px;
  color: var(--text-dim);
  font: inherit;
  font-size: 0.875rem;
  line-height: 1.2;
  background: none;
  border: 0;
  cursor: pointer;
}
.home-shortcut svg { width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.home-shortcut:hover, .home-shortcut:focus-visible { color: var(--text); outline: 2px solid var(--focus); outline-offset: 2px; border-radius: var(--radius); }

/* On Google TV and Android TV only the home rows shrink; the header stays full size. */
html.tv-ui .home-stage { display: block; flex: 1 1 auto; min-height: 0; position: relative; overflow: hidden; }
html.tv-ui .home-rows { position: absolute; top: 0; left: 0; width: 142.857%; height: 142.857%; transform: scale(0.7); transform-origin: 0 0; }

/* Narrow screens keep the icons and drop the labels (they stay as accessible names). */
@media (max-width: 900px) {
  .home-shortcut span { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .home-shortcut { padding: 8px; }
}
@media (max-width: 560px) {
  .topbar:has(.home-nav) { flex-wrap: wrap; }
  .topbar:has(.home-nav) .home-search { order: 3; flex: 1 0 100%; max-width: none; margin: 0; }
  .home-nav { margin-left: auto; }
}
</style>
