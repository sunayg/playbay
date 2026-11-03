// Subtitle and audio track state for the player, over the video's live
// TextTrackList/AudioTrackList (which Vue cannot observe, so every change
// event copies what it needs into refs).
import { ref, type Ref } from "vue";
import { storageGet, storageRemove, storageSet } from "../../lib/storage";
import { languageName, normalizeLanguage } from "./media";

export interface TrackChoice {
  index: number;
  label: string;
}

interface PlacedCue extends VTTCue {
  _rapidbayPlaced?: boolean;
  _rapidbayLine?: VTTCue["line"];
  _rapidbaySnap?: boolean;
  _rapidbayAlign?: VTTCue["lineAlign"];
  // Times as the subtitle file gave them, and the delay they are shifted by now.
  _rapidbayStart?: number;
  _rapidbayEnd?: number;
  _rapidbayShift?: number;
}
type AudioTrackListLike = EventTarget & { length: number; [index: number]: { language?: string; label?: string; enabled: boolean } };

const CAPTION_KEY = "captionLanguage";
const RAISED_LINE = 78; // percent from the top: clears the controls (default)
export const MAX_SUBTITLE_DELAY = 60; // seconds, either way
const DELAY_STEP = 0.05;
const SWEEP_MS = 1000;

export function clampDelay(seconds: number): number {
  const rounded = Math.round(seconds / DELAY_STEP) * DELAY_STEP;
  return Math.max(-MAX_SUBTITLE_DELAY, Math.min(MAX_SUBTITLE_DELAY, Math.round(rounded * 100) / 100));
}

export function useTextTracks(
  video: Ref<HTMLVideoElement | null>,
  isRaised: () => boolean,
  raisedLine: () => number = () => RAISED_LINE,
  // Where this file's subtitle delay is remembered; none means it is not.
  delayKey: () => string | null = () => null,
) {
  const captionChoices = ref<TrackChoice[]>([]);
  const activeCaption = ref<number | null>(null);
  const trackCount = ref(0);
  const currentCaption = ref<string | null>(null); // language of the showing track
  const audioChoices = ref<TrackChoice[]>([]);
  const activeAudio = ref<number | null>(null);
  // Seconds the subtitles are shifted later (negative: earlier).
  const delay = ref(0);
  let sweepTimer: ReturnType<typeof setInterval> | undefined;

  let attached: HTMLVideoElement | null = null;
  const bound = new WeakSet<TextTrack>();

  const textTracks = (): TextTrackList | null => (attached && attached.textTracks) || null;
  const audioTracks = (): AudioTrackListLike | null =>
    ((attached as unknown as { audioTracks?: AudioTrackListLike } | null)?.audioTracks) || null;

  function refresh(): void {
    const tracks = textTracks();
    const captions: TrackChoice[] = [];
    let active: number | null = null;
    let language: string | null = null;
    if (tracks) {
      for (let i = 0; i < tracks.length; i++) {
        const track = tracks[i];
        if (track.mode === "showing") language = track.language || null;
        if (track.kind !== "subtitles" && track.kind !== "captions") continue;
        captions.push({ index: i, label: languageName(track.language || track.label) || "Track " + (captions.length + 1) });
        if (track.mode === "showing") active = i;
      }
    }
    trackCount.value = tracks ? tracks.length : 0;
    captionChoices.value = captions;
    activeCaption.value = active;
    currentCaption.value = language;
    const audios: TrackChoice[] = [];
    let activeTrack: number | null = null;
    const list = audioTracks();
    if (list) {
      for (let j = 0; j < list.length; j++) {
        audios.push({ index: j, label: languageName(list[j].language || list[j].label) || "Track " + (j + 1) });
        if (list[j].enabled) activeTrack = j;
      }
    }
    audioChoices.value = audios;
    activeAudio.value = activeTrack;
  }

  // Lift the cues clear of the controls while they are shown.
  function placeCues(raised: boolean = isRaised()): void {
    const tracks = textTracks();
    if (!tracks) return;
    for (let i = 0; i < tracks.length; i++) {
      const cues = tracks[i].cues;
      if (!cues) continue;
      for (let j = 0; j < cues.length; j++) {
        const cue = cues[j] as PlacedCue;
        if (!cue._rapidbayPlaced) {
          cue._rapidbayLine = cue.line;
          cue._rapidbaySnap = cue.snapToLines;
          cue._rapidbayAlign = cue.lineAlign;
          cue._rapidbayPlaced = true;
        }
        if (raised) {
          cue.snapToLines = false;
          cue.lineAlign = "end";
          cue.line = raisedLine();
        } else {
          cue.line = cue._rapidbayLine as VTTCue["line"];
          cue.snapToLines = cue._rapidbaySnap as boolean;
          cue.lineAlign = cue._rapidbayAlign as VTTCue["lineAlign"];
        }
      }
    }
  }

  // Shift every cue by the delay. Cues of a stream arrive a few at a time as it plays, so this runs again
  // every second while a delay is set, and the new ones are caught before they come up.
  function applyDelay(): void {
    const tracks = textTracks();
    if (!tracks) return;
    for (let i = 0; i < tracks.length; i++) {
      const cues = tracks[i].cues;
      if (!cues) continue;
      for (let j = 0; j < cues.length; j++) {
        const cue = cues[j] as PlacedCue;
        if (cue._rapidbayStart === undefined) {
          cue._rapidbayStart = cue.startTime;
          cue._rapidbayEnd = cue.endTime;
          cue._rapidbayShift = 0;
        }
        if (cue._rapidbayShift === delay.value) continue;
        const start = Math.max(0, cue._rapidbayStart + delay.value);
        const end = Math.max(start, (cue._rapidbayEnd as number) + delay.value);
        // Later first for the end, so the cue never ends before it starts.
        if (end >= cue.endTime) {
          cue.endTime = end;
          cue.startTime = start;
        } else {
          cue.startTime = start;
          cue.endTime = end;
        }
        cue._rapidbayShift = delay.value;
      }
    }
  }

  function setDelay(seconds: number): void {
    delay.value = clampDelay(seconds);
    const key = delayKey();
    if (key) {
      if (delay.value) storageSet(key, String(delay.value));
      else storageRemove(key);
    }
    applyDelay();
    clearInterval(sweepTimer);
    sweepTimer = delay.value !== 0 && attached ? setInterval(applyDelay, SWEEP_MS) : undefined;
  }

  function nudgeDelay(step: number): void {
    setDelay(delay.value + step);
  }

  function loadDelay(): void {
    const key = delayKey();
    const saved = key ? Number(storageGet(key)) : 0;
    setDelay(Number.isFinite(saved) ? saved : 0);
  }

  function selectCaption(index: number | null): void {
    const tracks = textTracks();
    if (!tracks) return;
    let chosen = "";
    for (let i = 0; i < tracks.length; i++) {
      const on = index !== null && i === index;
      tracks[i].mode = on ? "showing" : "disabled";
      if (on) chosen = tracks[i].language || tracks[i].label || "";
    }
    if (chosen) storageSet(CAPTION_KEY, normalizeLanguage(chosen));
    else storageRemove(CAPTION_KEY);
    refresh();
  }

  function selectAudio(index: number): void {
    const list = audioTracks();
    if (!list) return;
    for (let i = 0; i < list.length; i++) list[i].enabled = i === index;
    refresh();
  }

  // The caption button: off -> first track -> next ... -> off.
  function cycleCaption(): void {
    const tracks = textTracks();
    if (!tracks) return;
    let current: number | null = null;
    // Every track at least "hidden" so its cues load.
    for (let i = 0; i < tracks.length; i++) {
      if (tracks[i].mode === "showing") current = i;
      else if (tracks[i].mode === "disabled") tracks[i].mode = "hidden";
    }
    if (current !== null) {
      tracks[current].mode = "hidden";
      if (tracks[current + 1]) tracks[current + 1].mode = "showing";
    } else if (tracks.length > 0) {
      tracks[0].mode = "showing";
    }
    refresh();
  }

  // Apply the remembered language only when nothing is showing: hls.js treats
  // any non-"disabled" track as the active subtitle, so blanket-hiding tracks
  // would auto-select whichever is last.
  function applyPreference(): void {
    const tracks = textTracks();
    const wanted = normalizeLanguage(storageGet(CAPTION_KEY));
    if (!tracks || !wanted) return;
    let showing = false;
    let match: TextTrack | null = null;
    for (let i = 0; i < tracks.length; i++) {
      if (tracks[i].mode === "showing") showing = true;
      if (!match && normalizeLanguage(tracks[i].language) === wanted) match = tracks[i];
    }
    if (!showing && match) match.mode = "showing";
  }

  function bindCueLift(track: TextTrack | null | undefined): void {
    if (!track || bound.has(track)) return;
    bound.add(track);
    track.addEventListener?.("cuechange", onCueChange);
  }
  function onCueChange(): void {
    placeCues();
  }

  // Several tracks can end up "showing" at once (Chromium auto-enables every
  // matching <track srclang>, and hls.js appends new TextTracks on loadSource
  // without removing the old). Keep the newest and hide the rest so no line is
  // drawn twice.
  function onChange(): void {
    const tracks = textTracks();
    if (!tracks) return;
    let last = -1;
    for (let i = 0; i < tracks.length; i++) if (tracks[i].mode === "showing") last = i;
    let language: string | null = null;
    for (let j = 0; j < tracks.length; j++) {
      if (tracks[j].mode !== "showing") continue;
      if (j !== last) tracks[j].mode = "hidden";
      else language = tracks[j].language;
    }
    if (language) storageSet(CAPTION_KEY, normalizeLanguage(language));
    else storageRemove(CAPTION_KEY);
    refresh();
  }
  function onAddTrack(event: Event): void {
    bindCueLift((event as TrackEvent).track as TextTrack | null);
    applyPreference();
    refresh();
    placeCues();
  }
  function onTrackListChange(): void {
    refresh();
  }
  function onAddAudio(): void {
    refresh();
  }

  function attach(): void {
    const el = video.value;
    if (!el || attached) return;
    attached = el;
    const tracks = textTracks();
    if (tracks) {
      for (let i = 0; i < tracks.length; i++) {
        tracks[i].mode = "disabled";
        bindCueLift(tracks[i]);
      }
      applyPreference();
      tracks.addEventListener?.("addtrack", onAddTrack);
      tracks.addEventListener?.("removetrack", onTrackListChange);
      tracks.addEventListener?.("change", onChange);
    }
    audioTracks()?.addEventListener?.("addtrack", onAddAudio);
    refresh();
    loadDelay();
  }

  function detach(): void {
    clearInterval(sweepTimer);
    sweepTimer = undefined;
    const tracks = textTracks();
    if (tracks) {
      tracks.removeEventListener?.("addtrack", onAddTrack);
      tracks.removeEventListener?.("removetrack", onTrackListChange);
      tracks.removeEventListener?.("change", onChange);
      for (let i = 0; i < tracks.length; i++) tracks[i].removeEventListener?.("cuechange", onCueChange);
    }
    audioTracks()?.removeEventListener?.("addtrack", onAddAudio);
    attached = null;
  }

  return {
    captionChoices,
    activeCaption,
    trackCount,
    currentCaption,
    audioChoices,
    activeAudio,
    delay,
    setDelay,
    nudgeDelay,
    refresh,
    placeCues,
    selectCaption,
    selectAudio,
    cycleCaption,
    attach,
    detach,
  };
}
