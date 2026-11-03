// Keyboard, remote and media-key control of the player.
//
// Media keys (play/pause, fast-forward, next, back/escape) go through the
// shared focus module's media handler. The plain keys (arrows, Enter, Space,
// K, M, F, C) are taken on window in the capture phase, ahead of the shared
// spatial-focus handler, and only while the video itself (or nothing) has
// focus, so arrows still move between the controls once one is focused.
import { onBeforeUnmount } from "vue";
import { arrowDirection, mediaKeyAction, registerMediaKeyHandler, type MediaAction } from "../../lib/focus";

export const SEEK_FORWARD = 60;
export const SEEK_BACK = 15;
export const SEEK_FAST_FORWARD = 30;
export const SUBTITLE_STEP = 0.25;

export interface PlayerActions {
  playPause(wanted?: "play" | "pause"): void;
  seekBy(seconds: number): void;
  toggleMute(): void;
  toggleFullscreen(): void;
  cycleCaption(): void;
  // Show the subtitles earlier (negative) or later, by this many seconds.
  nudgeSubtitles(seconds: number): void;
  playNext(): void;
  hasNext(): boolean;
  menuOpen(): boolean;
  closeMenu(): void;
  openRawVideo(): boolean;
  // Show the controls (and restart their auto-hide timer).
  reveal(): void;
}

function videoHasFocus(): boolean {
  const tag = document.activeElement && document.activeElement.tagName.toLowerCase();
  return tag === "video" || tag === "body";
}

export function usePlayerKeys(actions: PlayerActions): void {
  function onMedia(action: MediaAction): boolean {
    switch (action) {
      case "playpause":
        actions.playPause();
        return true;
      case "play":
      case "pause":
        actions.playPause(action);
        return true;
      case "fastforward":
        actions.seekBy(SEEK_FAST_FORWARD);
        return true;
      case "next":
        if (!actions.hasNext()) return false;
        actions.playNext();
        return true;
      case "back":
        if (!actions.menuOpen()) return false;
        actions.closeMenu();
        return true;
      case "rewind":
        if (!videoHasFocus()) return false;
        // Legacy remote shortcut: open the file itself. Only possible for a
        // plain src; a stream (blob: URL) just rewinds.
        if (!actions.openRawVideo()) actions.seekBy(-SEEK_FAST_FORWARD);
        return true;
    }
    return false;
  }

  function onKeydown(event: KeyboardEvent): void {
    // Any key shows the controls, handled here or by the shared handlers
    // (which stop the event before a bubbling listener could see it).
    actions.reveal();
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (mediaKeyAction(event)) return; // the shared media handler owns these
    if (!videoHasFocus()) return;
    const key = String(event.key || "").toLowerCase();
    const code = event.keyCode || event.which || 0;
    const direction = arrowDirection(event);
    const handled = (fn: () => void) => {
      event.preventDefault();
      event.stopPropagation();
      fn();
    };
    if (direction === "right") handled(() => actions.seekBy(SEEK_FORWARD));
    else if (direction === "left") handled(() => actions.seekBy(-SEEK_BACK));
    else if (key === "enter" || key === "select" || code === 13 || code === 23 || key === " " || key === "spacebar" || key === "k") handled(() => actions.playPause());
    else if (key === "m") handled(() => actions.toggleMute());
    else if (key === "f") handled(() => actions.toggleFullscreen());
    else if (key === "c") handled(() => actions.cycleCaption());
    else if (key === "[") handled(() => actions.nudgeSubtitles(-SUBTITLE_STEP));
    else if (key === "]") handled(() => actions.nudgeSubtitles(SUBTITLE_STEP));
  }

  const unregister = registerMediaKeyHandler((action) => onMedia(action));
  window.addEventListener("keydown", onKeydown, true);
  onBeforeUnmount(() => {
    unregister();
    window.removeEventListener("keydown", onKeydown, true);
  });
}
