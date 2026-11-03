import type { ActivityDownload, ActivityStream } from "../types";

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit++;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

export function formatAge(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  if (days >= 1) return `${days} day${days === 1 ? "" : "s"} ago`;
  const hours = Math.floor(seconds / 3600);
  if (hours >= 1) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return "just now";
}

export function streamSummary(stream: ActivityStream): string {
  const parts = [stream.levels.map((h) => `${h}p`).join(" ") || "no video", stream.encoder, stream.hdr !== "sdr" ? "HDR to SDR" : ""];
  return parts.filter(Boolean).join(" · ");
}

export function streamState(stream: ActivityStream): string {
  if (stream.failed) return "The encoder gave up";
  if (stream.paused) return "Waiting for you: encoded far enough ahead";
  if (stream.speed === null) return stream.idle_seconds > 30 ? `Idle for ${formatDuration(stream.idle_seconds)}` : "Starting";
  const behind = stream.speed < 1 ? " (too slow to keep up)" : "";
  return `Encoding at ${stream.speed.toFixed(1)}× real time${behind}`;
}

export function downloadState(item: ActivityDownload): string {
  if (item.status === "ready") {
    const parts = [item.size !== null ? formatBytes(item.size) : "", item.age_seconds !== null ? `kept since ${formatAge(item.age_seconds)}` : ""];
    return parts.filter(Boolean).join(" · ");
  }
  if (item.status === "downloading") {
    const parts = [
      item.progress !== null ? `${Math.round(item.progress * 100)}%` : "",
      item.download_rate ? `${formatBytes(item.download_rate)}/s` : "",
      item.peers ? `${item.peers} peer${item.peers === 1 ? "" : "s"}` : "",
    ];
    return parts.filter(Boolean).join(" · ") || "Downloading";
  }
  return (item.status ?? "waiting").replace(/_/g, " ").toLowerCase();
}
