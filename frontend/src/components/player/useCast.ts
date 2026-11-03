// Chromecast support. chromecastjs and Google's cast sender script are plain
// global scripts; they are injected only when a cast button will be shown, so
// other browsers never fetch them. A script that fails to load (blocked,
// offline) is not an error: the cast button just does not appear.
import { get } from "../../lib/api";

export interface CastSubtitle {
  active: boolean;
  src: string;
  label: string;
  srclang: string;
}
export interface CastMedia {
  content: string;
  contentType: string;
  title: string;
  subtitles: CastSubtitle[];
  time: number;
  paused: boolean;
  poster: null;
  description: null;
}
export interface ChromecastLike {
  cast(media: CastMedia, done: (problem: unknown) => void): void;
  on?(event: string, callback: (detail: unknown) => void): void;
}
export type CastLoader = () => Promise<ChromecastLike | null>;

const SENDER_SRC = "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1";

function localScript(): string {
  return (import.meta.env.BASE_URL || "/") + "lib/chromecastjs.js";
}

function injectScript(src: string): Promise<boolean> {
  return new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = () => resolve(true);
    script.onerror = () => {
      script.remove();
      resolve(false);
    };
    document.head.appendChild(script);
  });
}

// One cast handle for the page: each ChromecastJS polls for the SDK and
// registers its own player listeners, so it is made once and reused.
let loading: Promise<ChromecastLike | null> | null = null;

export function loadCast(): Promise<ChromecastLike | null> {
  if (!loading) {
    loading = (async () => {
      const w = window as unknown as { ChromecastJS?: new () => ChromecastLike };
      if (!w.ChromecastJS && !(await injectScript(localScript()))) return null;
      if (!w.ChromecastJS) return null;
      // The sender script is optional for loading; without it casting just
      // reports "not available" when used.
      void injectScript(SENDER_SRC);
      return new w.ChromecastJS();
    })();
  }
  return loading;
}

export function resetCastForTests(): void {
  loading = null;
}

const LOCAL_HOST = /^(localhost|127(\.\d+){3}|\[?::1\]?)$/;

export interface CastRequest {
  videoUrl: string;
  // The torrent's hash, for a video played from an encode session (its URL does not carry it).
  magnetHash?: string;
  title: string;
  video: HTMLVideoElement | null;
  cc: ChromecastLike | null;
}

// "Not available" says nothing about why, so add what the page can see of the cast SDK.
function explain(problem: string): string {
  if (!/not available/i.test(problem)) return problem;
  const cast = (window as unknown as { chrome?: { cast?: { isAvailable?: boolean } } }).chrome?.cast;
  const sdk = !cast ? "cast script not loaded" : cast.isAvailable ? "cast script ready" : "cast script loaded but not ready";
  return `${problem} (${window.isSecureContext ? "https" : "not https"}, ${sdk})`;
}

// Resolves to an error message to show, or null when the cast started.
export async function castVideo(request: CastRequest): Promise<string | null> {
  const { cc, video } = request;
  if (!cc || !video) return "Chromecast is not available in this browser.";
  if (LOCAL_HOST.test(window.location.hostname)) {
    return "Open PlayBay by its network address to cast. A TV cannot reach localhost.";
  }
  const session = /^\/api\/hls\/([^/]+)\/master\.m3u8$/.exec(request.videoUrl || "");
  const parts = session ? (request.magnetHash ? [request.videoUrl, request.magnetHash] : null) : /^\/play\/([^/]+)\/(.*)$/.exec(request.videoUrl || "");
  if (!parts) return "This video cannot be cast.";
  let prefix: string;
  try {
    // The TV cannot send the password, so it gets a signed link for this torrent.
    const link = await get<{ prefix: string; origin?: string }>("/api/cast_link/" + parts[1]);
    prefix = (link.origin || window.location.origin) + link.prefix;
  } catch {
    return "Could not prepare a cast link.";
  }
  const castUrl = (path: string): string | null => {
    const inner = /^\/play\/[^/]+\/(.*)$/.exec(path || "");
    return inner ? prefix + inner[1] : null;
  };
  const subtitles: CastSubtitle[] = [];
  for (let i = 0; i < video.textTracks.length; i++) {
    const track = video.textTracks[i];
    const src = castUrl(track.id);
    if (src) subtitles.push({ active: track.mode === "showing", src, label: track.label, srclang: track.language });
  }
  return new Promise((resolve) => {
    cc.cast(
      {
        content: session ? prefix + "session/" + session[1] + "/master.m3u8" : (castUrl(request.videoUrl) as string),
        contentType: session ? "application/x-mpegURL" : "video/mp4",
        title: request.title || "PlayBay",
        subtitles,
        time: video.currentTime || 0,
        paused: false,
        poster: null,
        description: null,
      },
      (problem) => {
        if (problem) return resolve(explain(String(problem)));
        // The TV plays from here, so stop the local copy.
        if (!video.paused) video.pause();
        resolve(null);
      },
    );
  });
}
