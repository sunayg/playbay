// Keyboard, TV remote and media-key navigation, ported from the legacy app
// (keylistener, focusByDirection, arrowDirection) without jQuery.

export type Direction = "up" | "down" | "left" | "right";
export type MediaAction = "playpause" | "play" | "pause" | "fastforward" | "rewind" | "next" | "back";

export const TV_USER_AGENT = /Android ?TV|Google ?TV|\bTV\b|BRAVIA|Chromecast|MiBOX|MiTV-|Nexus Player|Leanback/i;

// The home screen's own targets; its rows scroll to keep the focus in view.
export const HOME_TARGET_SELECTOR = ".form-control, .billboard-card, .poster-card, .search-chip, .search-history-clear, .home-shortcut";
export const FOCUSABLE_SELECTOR =
  'a[href], button, input, select, textarea, summary, [tabindex], [contenteditable=""], [contenteditable="true"]';

const NON_TEXT_INPUT_TYPES = ["button", "checkbox", "radio", "range", "submit", "reset", "file", "image", "color"];

export function isTextEntry(el: Element | null): boolean {
  if (!el) return false;
  const html = el as HTMLElement;
  if (el.tagName === "TEXTAREA" || html.isContentEditable) return true;
  if (el.tagName !== "INPUT") return false;
  return NON_TEXT_INPUT_TYPES.indexOf(((el as HTMLInputElement).type || "text").toLowerCase()) === -1;
}

// Hisense VIDAA's built-in browser: Chromium on Linux, which says "WebExplorer/<version>" and no TV word.
export const VIDAA_USER_AGENT = /WebExplorer\/|VIDAA|Hisense/i;

export function isTvUserAgent(ua: string): boolean {
  return (/Android/i.test(ua) && TV_USER_AGENT.test(ua)) || VIDAA_USER_AGENT.test(ua);
}

export function applyTvMode(ua: string = navigator.userAgent || ""): boolean {
  const tv = isTvUserAgent(ua);
  if (tv) document.documentElement.classList.add("tv-ui");
  return tv;
}

export function arrowDirection(e: Pick<KeyboardEvent, "key" | "keyCode" | "which" | "code">): Direction | "" {
  const code = e.keyCode || e.which || 0;
  // Android TV and Google TV remotes use these codes for the D-pad.
  if (code === 19) return "up";
  if (code === 20) return "down";
  if (code === 21) return "left";
  if (code === 22) return "right";
  const key = String(e.key || "").toLowerCase();
  if (key === "arrowup" || key === "up") return "up";
  if (key === "arrowdown" || key === "down") return "down";
  if (key === "arrowleft" || key === "left") return "left";
  if (key === "arrowright" || key === "right") return "right";
  if (code === 38) return "up";
  if (code === 40) return "down";
  if (code === 37) return "left";
  if (code === 39) return "right";
  const domCode = String(e.code || "").toLowerCase();
  if (domCode === "arrowup") return "up";
  if (domCode === "arrowdown") return "down";
  if (domCode === "arrowleft") return "left";
  if (domCode === "arrowright") return "right";
  return "";
}

export function isSelectKey(e: Pick<KeyboardEvent, "key" | "keyCode" | "which">): boolean {
  const name = String(e.key || "").toLowerCase();
  const code = e.keyCode || e.which || 0;
  return name === "enter" || name === "select" || code === 13 || code === 23;
}

export function mediaKeyAction(e: Pick<KeyboardEvent, "key" | "keyCode" | "which">): MediaAction | null {
  const key = String(e.key || "").toLowerCase();
  const code = e.keyCode || e.which || 0;
  if (key === "mediaplaypause" || code === 179) return "playpause";
  if (key === "mediaplay" || code === 126) return "play";
  if (key === "mediapause" || code === 127) return "pause";
  if (key === "mediafastforward" || code === 417) return "fastforward";
  if (key === "mediarewind") return "rewind";
  if (key === "mediatracknext" || code === 176) return "next";
  // 4: Android KEYCODE_BACK, 461: webOS Back, 10009: Tizen Back.
  if (key === "escape" || key === "goback" || key === "browserback" || code === 4 || code === 461 || code === 10009) return "back";
  return null;
}

type MediaHandler = (action: MediaAction, event: KeyboardEvent) => boolean | void;
const mediaHandlers: MediaHandler[] = [];

// The player registers here; the most recent handler gets the key first and
// returns true when it consumed it. Returns an unregister function.
export function registerMediaKeyHandler(handler: MediaHandler): () => void {
  mediaHandlers.push(handler);
  return () => {
    const index = mediaHandlers.indexOf(handler);
    if (index !== -1) mediaHandlers.splice(index, 1);
  };
}

// ---- spatial choice ---------------------------------------------------------

export interface Place<T = Element> {
  el: T;
  x: number;
  y: number;
}

const MIN_STEP = 24;
const ROW_BAND = 36;

// Picks the neighbour in a direction: the nearest row (or column band) in that
// direction, then the horizontally closest item in it. Pure, so it is testable
// without layout.
export function pickByDirection<T>(fromX: number, fromY: number, places: Place<T>[], direction: Direction): T | null {
  let best: T | null = null;
  let bestScore = Infinity;
  if (direction === "down" || direction === "up") {
    let band: number | null = null;
    for (const place of places) {
      const dy = place.y - fromY;
      if (direction === "down" && dy < MIN_STEP) continue;
      if (direction === "up" && dy > -MIN_STEP) continue;
      if (band === null || (direction === "down" && place.y < band) || (direction === "up" && place.y > band)) band = place.y;
    }
    if (band === null) return null;
    for (const place of places) {
      if (Math.abs(place.y - band) > ROW_BAND) continue;
      const score = Math.abs(place.x - fromX);
      if (score < bestScore) {
        bestScore = score;
        best = place.el;
      }
    }
  } else {
    for (const place of places) {
      if (Math.abs(place.y - fromY) > ROW_BAND) continue;
      const dx = place.x - fromX;
      if (direction === "right" && dx < MIN_STEP) continue;
      if (direction === "left" && dx > -MIN_STEP) continue;
      const score = Math.abs(dx);
      if (score < bestScore) {
        bestScore = score;
        best = place.el;
      }
    }
  }
  return best;
}

// ---- DOM helpers ----------------------------------------------------------------

function isVisible(el: Element): boolean {
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

export function focusableElements(root: ParentNode = document): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter((el) => {
    if (el.getAttribute("tabindex") === "-1") return false;
    if ((el as HTMLInputElement).disabled) return false;
    if (el.hasAttribute("hidden") || el.closest("[hidden]")) return false;
    if (el instanceof HTMLInputElement && el.type === "hidden") return false;
    return isVisible(el);
  });
}

function homeTargets(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>(HOME_TARGET_SELECTOR)).filter(
    (el) => !(el as HTMLInputElement).disabled && isVisible(el),
  );
}

function centre(el: Element): Place {
  const rect = el.getBoundingClientRect();
  return { el, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
}

export function revealHomeTarget(el: Element): void {
  const scroller = el.closest(".billboard-scroller") as HTMLElement | null;
  if (scroller) {
    const item = el.closest(".rank-item") || el;
    const itemRect = item.getBoundingClientRect();
    const scrollerRect = scroller.getBoundingClientRect();
    if (itemRect.left < scrollerRect.left + 28) {
      scroller.scrollLeft -= scrollerRect.left + 28 - itemRect.left;
    } else if (itemRect.right > scrollerRect.right - 28) {
      scroller.scrollLeft += itemRect.right - (scrollerRect.right - 28);
    }
  }
  const rows = document.querySelector<HTMLElement>(".home-rows");
  if (rows && rows.contains(el)) {
    const section = el.closest(".home-row, .home-shortcuts") || el;
    const heading = section.querySelector(".home-row-heading");
    const rowsRect = rows.getBoundingClientRect();
    const sectionRect = section.getBoundingClientRect();
    const desiredMid = rowsRect.top + rowsRect.height * 0.4;
    let delta = sectionRect.top + sectionRect.height / 2 - desiredMid;
    if (heading) {
      const headingTop = heading.getBoundingClientRect().top - delta;
      const minTop = rowsRect.top + 12;
      if (headingTop < minTop) delta -= minTop - headingTop;
    }
    const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
    rows.scrollTop = Math.min(maxScroll, Math.max(0, rows.scrollTop + delta));
  }
}

function firstInNextRow(current: Element, targets: HTMLElement[]): HTMLElement | null {
  const rows = document.querySelector(".home-rows");
  if (!rows) return null;
  const fromBottom = current.getBoundingClientRect().bottom;
  for (const section of Array.from(rows.querySelectorAll(".home-row, .home-shortcuts"))) {
    if (section.getBoundingClientRect().top < fromBottom - 8) continue;
    const hit = targets.find((target) => section.contains(target));
    if (hit) return hit;
  }
  return null;
}

function focusAndReveal(el: HTMLElement): void {
  el.focus();
  revealHomeTarget(el);
}

// Moves focus one step. On the home screen only its own targets count (and Down
// from the search box jumps to the next row); elsewhere every focusable does,
// falling back to document order when nothing lies in that direction.
export function focusByDirection(direction: Direction): boolean {
  const current = document.activeElement as HTMLElement | null;
  const onHome = document.querySelector(".home-rows") !== null;
  const targets = onHome ? homeTargets() : focusableElements();
  if (!current || current === document.body || targets.indexOf(current) === -1) {
    if (targets[0]) {
      focusAndReveal(targets[0]);
      return true;
    }
    return false;
  }
  if (onHome && direction === "down" && current.classList.contains("form-control")) {
    const next = firstInNextRow(current, targets);
    if (next) {
      focusAndReveal(next);
      return true;
    }
  }
  const from = centre(current);
  let places = targets.filter((el) => el !== current).map((el) => centre(el) as Place<HTMLElement>);
  const rows = document.querySelector(".home-rows");
  if (rows && rows.contains(current) && (direction === "up" || direction === "down")) {
    const inRows = places.filter((place) => rows.contains(place.el));
    const hasRowTarget = inRows.some((place) => {
      const dy = place.y - from.y;
      return direction === "down" ? dy >= MIN_STEP : dy <= -MIN_STEP;
    });
    if (hasRowTarget) places = inRows;
  }
  let best = pickByDirection(from.x, from.y, places, direction);
  if (!best && !onHome) {
    const index = targets.indexOf(current);
    best = direction === "down" || direction === "right" ? (targets[index + 1] ?? null) : (targets[index - 1] ?? null);
  }
  if (!best) return false;
  focusAndReveal(best);
  return true;
}

// ---- the key handler -----------------------------------------------------------------

export function handleKeydown(e: KeyboardEvent): void {
  const active = document.activeElement as HTMLElement | null;
  const typing = isTextEntry(active);
  const direction = arrowDirection(e);
  const lower = String(e.key || "").toLowerCase();

  const media = mediaKeyAction(e);
  if (media) {
    for (let i = mediaHandlers.length - 1; i >= 0; i--) {
      if (mediaHandlers[i](media, e) === true) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
    }
  }

  // A Back key nobody consumed (no menu to close) leaves the screen like Backspace does. Escape only
  // does so on a TV, where it is the remote's Back; on a desktop it would be a surprise.
  if (media === "back") {
    const isEscape = lower === "escape";
    if (!isEscape || (!typing && document.documentElement.classList.contains("tv-ui"))) {
      e.preventDefault();
      e.stopPropagation();
      window.history.back();
      return;
    }
  }

  // In a text field, Backspace edits and left/right move the caret; up and
  // down still leave the field.
  // A field marked data-exit-right lets ArrowRight leave once the caret is at
  // the end of the text, so the remote can reach the buttons beside it.
  const atEnd =
    active instanceof HTMLInputElement &&
    active.dataset.exitRight !== undefined &&
    active.selectionStart === active.value.length &&
    active.selectionEnd === active.value.length;
  if (typing && (lower === "backspace" || direction === "left" || (direction === "right" && !atEnd))) return;

  if (isSelectKey(e)) {
    if (active && active.tagName === "INPUT") return;
    if (active && active !== document.body) {
      e.preventDefault();
      active.click();
    }
    return;
  }
  if (direction) {
    e.preventDefault();
    e.stopPropagation();
    focusByDirection(direction);
    return;
  }
  if (lower === "backspace") {
    e.preventDefault();
    e.stopPropagation();
    window.history.back();
  }
}

// A moving mouse takes over from the remote: drop the focus ring.
function handleMouseMove(): void {
  const active = document.activeElement as HTMLElement | null;
  if (active && active.tagName !== "INPUT" && active !== document.body) active.blur();
}

let installed = false;

export function installFocus(): void {
  if (installed) return;
  installed = true;
  applyTvMode();
  document.addEventListener("keydown", handleKeydown, true);
  document.addEventListener("mousemove", handleMouseMove);
}

export function uninstallFocus(): void {
  if (!installed) return;
  installed = false;
  document.removeEventListener("keydown", handleKeydown, true);
  document.removeEventListener("mousemove", handleMouseMove);
}
