import { storageGet, storageSet } from "./storage";

const KEY = "playbay.viewer";
let cached = "";

// Names this device to the server, which gives each viewer of a file an encoder of their own. Kept across
// reloads so a reload rejoins its own session; made again when storage is unavailable.
export function viewerId(): string {
  if (cached) return cached;
  let id = storageGet(KEY) || "";
  if (!/^[A-Za-z0-9_-]{8,40}$/.test(id)) {
    id = Array.from({ length: 16 }, () => Math.floor(Math.random() * 36).toString(36)).join("");
    storageSet(KEY, id);
  }
  cached = id;
  return id;
}

export function resetViewerForTests(): void {
  cached = "";
}
