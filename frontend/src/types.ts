export type Kind =
  | "bool"
  | "int"
  | "str"
  | "url"
  | "bitrate"
  | "str_list"
  | "int_list"
  | "host_port_list"
  | "regex_list"
  | "choice";

export interface FieldInfo {
  key: string;
  category: string;
  kind: Kind;
  // The options of a "choice".
  choices?: { value: string; label: string }[];
  label: string;
  help: string;
  secret: boolean;
  restart_needed: boolean;
  nullable: boolean;
  min: number | null;
  max: number | null;
  default: unknown;
  value: unknown;
  is_set: boolean | null;
  source: "saved" | "env" | "default";
  pending_restart: boolean;
}

export interface SettingsPayload {
  categories: { id: string; label: string }[];
  fields: FieldInfo[];
}

export type SaveResult =
  | { ok: true; payload: SettingsPayload }
  | { ok: false; errors: Record<string, string> };

export interface HlsDiagnostics {
  ffmpeg: string;
  hwaccels: string[];
  cpu_count: number;
  gpu_device: { path: string; present: boolean };
  encoder: { setting: string; chosen: string; low_power: boolean };
  decode: { setting: string; on_gpu: boolean };
  attempts: { what: string; worked: boolean; detail: string }[];
  speed: { state: string; pixels_per_second: number | null; full_ladder_speed: number | null; target: number; error: string };
  configured_heights: number[];
  warnings: string[];
}

export interface ActivityStream {
  id: string;
  hash: string;
  filename: string;
  encoder: string;
  hdr: string;
  levels: number[];
  duration: number;
  position_seconds: number;
  encoded_seconds: number;
  paused: boolean;
  speed: number | null;
  restarts: number;
  idle_seconds: number;
  failed: boolean;
}

export interface ActivityDownload {
  hash: string;
  filename: string;
  status: string | null;
  progress: number | null;
  peers: number | null;
  download_rate: number | null;
  size: number | null;
  age_seconds: number | null;
}

export interface Activity {
  streams: ActivityStream[];
  downloads: ActivityDownload[];
  disk: { total?: number; free?: number };
}
