import { storageGet } from "./storage";
import type { HlsDiagnostics, SaveResult, SettingsPayload } from "../types";

async function request(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  const cookie = response.headers.get("x-set-cookie");
  if (cookie) document.cookie = cookie;
  // The server answers 404 when the password cookie is missing or wrong.
  if (response.status === 404) {
    window.location.assign("/");
    throw new Error("Not authorized");
  }
  return response;
}

async function payloadOrThrow(response: Response): Promise<SettingsPayload> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const errors = (body as { errors?: Record<string, string> }).errors;
    throw new Error(errors ? Object.values(errors).join(", ") : `Request failed (${response.status})`);
  }
  return response.json();
}

export async function getSettings(): Promise<SettingsPayload> {
  return payloadOrThrow(await request("/api/settings"));
}

export async function getHlsDiagnostics(): Promise<HlsDiagnostics> {
  const response = await request("/api/hls_diagnostics");
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

export async function saveCategory(category: string, values: Record<string, unknown>): Promise<SaveResult> {
  const response = await request(`/api/settings/${encodeURIComponent(category)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values }),
  });
  if (response.status === 422) {
    const body = (await response.json()) as { errors: Record<string, string> };
    return { ok: false, errors: body.errors };
  }
  return { ok: true, payload: await payloadOrThrow(response) };
}

export async function resetField(key: string): Promise<SettingsPayload> {
  return payloadOrThrow(await request(`/api/settings/${encodeURIComponent(key)}/reset`, { method: "POST" }));
}

export async function clearField(key: string): Promise<SettingsPayload> {
  return payloadOrThrow(await request(`/api/settings/${encodeURIComponent(key)}/clear`, { method: "POST" }));
}

// ---------------------------------------------------------------------------
// Generic helpers ported from the legacy app (get/post/subscribe and the
// pending-request bookkeeping that route changes use to drop stale work).

const pendingControllers = new Set<AbortController>();
const pendingTimeouts = new Set<ReturnType<typeof setTimeout>>();

export function restoreCookie(): void {
  // Legacy: the password cookie is mirrored in localStorage and restored if lost.
  if (!document.cookie) {
    const saved = storageGet("cookie");
    if (saved) document.cookie = saved;
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function jsonOrThrow<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return (await response.json()) as T;
}

export async function get<T = unknown>(url: string): Promise<T> {
  restoreCookie();
  const controller = new AbortController();
  pendingControllers.add(controller);
  try {
    return await jsonOrThrow<T>(await request(url, { signal: controller.signal }));
  } finally {
    pendingControllers.delete(controller);
  }
}

// Form-encoded like jQuery's $.post.
export async function post<T = unknown>(url: string, data: Record<string, string | number | boolean> = {}): Promise<T> {
  restoreCookie();
  const body = new URLSearchParams();
  for (const [key, value] of Object.entries(data)) body.append(key, String(value));
  return jsonOrThrow<T>(await request(url, { method: "POST", body }));
}

export async function postJSON<T = unknown>(url: string, data: unknown): Promise<T> {
  restoreCookie();
  return jsonOrThrow<T>(
    await request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }),
  );
}

export function clearPendingRequests(): void {
  for (const controller of pendingControllers) controller.abort();
  pendingControllers.clear();
}

// Like the legacy rbsetTimeout: only one is live, and route changes drop it.
export function routeTimeout(fn: () => void, ms: number): void {
  clearPendingTimeouts();
  pendingTimeouts.add(setTimeout(fn, ms));
}

export function clearPendingTimeouts(): void {
  for (const timer of pendingTimeouts) clearTimeout(timer);
  pendingTimeouts.clear();
}

export function clearPending(): void {
  clearPendingRequests();
  clearPendingTimeouts();
}

// Subscribe to a server-sent-events endpoint. Calls callback per event, closes
// on {done: true}, and invokes fallback (e.g. to start polling) if SSE is
// unavailable or the stream errors.
export function subscribe<T extends { done?: boolean } = { done?: boolean }>(
  url: string,
  callback: (data: T) => void,
  fallback: () => void,
): EventSource | null {
  if (typeof window.EventSource === "undefined") {
    fallback();
    return null;
  }
  restoreCookie();
  const source = new EventSource(url);
  source.onmessage = (event) => {
    const data = JSON.parse(event.data) as T;
    if (data.done) source.close();
    callback(data);
  };
  source.onerror = () => {
    source.close();
    fallback();
  };
  return source;
}
