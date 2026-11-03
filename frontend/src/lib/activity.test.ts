import { describe, expect, it } from "vitest";
import type { ActivityDownload, ActivityStream } from "../types";
import { downloadState, formatAge, formatBytes, formatDuration, streamState, streamSummary } from "./activity";

const stream = (over: Partial<ActivityStream> = {}): ActivityStream => ({
  id: "s", hash: "a".repeat(40), filename: "movie.mkv", encoder: "h264_vaapi", hdr: "sdr", levels: [1080, 720],
  duration: 3600, position_seconds: 60, encoded_seconds: 120, paused: false, speed: 2.04, restarts: 0, idle_seconds: 0, failed: false, ...over,
});
const item = (over: Partial<ActivityDownload> = {}): ActivityDownload => ({
  hash: "a".repeat(40), filename: "movie.mkv", status: "downloading", progress: 0.42, peers: 5, download_rate: 2_500_000, size: null, age_seconds: null, ...over,
});

describe("formatting", () => {
  it("writes durations, sizes and ages for people", () => {
    expect(formatDuration(75)).toBe("1:15");
    expect(formatDuration(3725)).toBe("1:02:05");
    expect(formatDuration(-3)).toBe("0:00");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1_500_000)).toBe("1.5 MB");
    expect(formatBytes(250_000_000_000)).toBe("250 GB");
    expect(formatAge(30)).toBe("just now");
    expect(formatAge(7200)).toBe("2 hours ago");
    expect(formatAge(86400)).toBe("1 day ago");
  });
});

describe("what a stream is doing", () => {
  it("names the levels, the encoder and HDR", () => {
    expect(streamSummary(stream())).toBe("1080p 720p · h264_vaapi");
    expect(streamSummary(stream({ hdr: "pq" }))).toContain("HDR to SDR");
  });

  it("says how fast it encodes and warns when it is too slow", () => {
    expect(streamState(stream())).toBe("Encoding at 2.0× real time");
    expect(streamState(stream({ speed: 0.6 }))).toContain("too slow to keep up");
  });

  it("explains a pause, an idle session and a dead encoder", () => {
    expect(streamState(stream({ paused: true, speed: null }))).toContain("encoded far enough ahead");
    expect(streamState(stream({ speed: null, idle_seconds: 90 }))).toBe("Idle for 1:30");
    expect(streamState(stream({ speed: null }))).toBe("Starting");
    expect(streamState(stream({ failed: true }))).toBe("The encoder gave up");
  });
});

describe("what a download is doing", () => {
  it("shows progress, speed and peers while downloading", () => {
    expect(downloadState(item())).toBe("42% · 2.5 MB/s · 5 peers");
    expect(downloadState(item({ peers: 1, download_rate: 0 }))).toBe("42% · 1 peer");
  });

  it("shows the size and age of a kept file, and other states in words", () => {
    expect(downloadState(item({ status: "ready", size: 1_500_000_000, age_seconds: 7200 }))).toBe("1.5 GB · kept since 2 hours ago");
    expect(downloadState(item({ status: "waiting_for_metadata" }))).toBe("waiting for metadata");
  });
});
