import { createRouter, createWebHistory, type RouterHistory } from "vue-router";
import { clearPending } from "./lib/api";
import ActivityScreen from "./screens/ActivityScreen.vue";
import DownloadScreen from "./screens/DownloadScreen.vue";
import FilelistScreen from "./screens/FilelistScreen.vue";
import HomeScreen from "./screens/HomeScreen.vue";
import SearchResultsScreen from "./screens/SearchResultsScreen.vue";
import SettingsScreen from "./screens/SettingsScreen.vue";
import TorrentLinkScreen from "./screens/TorrentLinkScreen.vue";

export function createAppRouter(history: RouterHistory = createWebHistory()) {
  const router = createRouter({
    history,
    routes: [
      { path: "/", name: "home", component: HomeScreen },
      { path: "/search/:searchterm?", name: "search", component: SearchResultsScreen },
      { path: "/torrent/:torrent_link/", name: "torrent", component: TorrentLinkScreen },
      { path: "/magnet/:magnet_link/", name: "filelist", component: FilelistScreen },
      { path: "/magnet/:magnet_link/:filename", name: "download", component: DownloadScreen },
      { path: "/settings", name: "settings", component: SettingsScreen },
      { path: "/activity", name: "activity", component: ActivityScreen },
      // Legacy: anything unknown shows the home screen.
      { path: "/:pathMatch(.*)*", name: "fallback", component: HomeScreen },
    ],
  });
  // Stale requests and timers from the screen we are leaving are dropped.
  router.beforeEach(() => {
    clearPending();
  });
  return router;
}
