<script setup lang="ts">
import { ref } from "vue";
import { getHlsDiagnostics } from "../lib/api";
import type { HlsDiagnostics } from "../types";

const report = ref<HlsDiagnostics | null>(null);
const error = ref("");

async function load(event: Event): Promise<void> {
  if (!(event.target as HTMLDetailsElement).open) return;
  error.value = "";
  try {
    report.value = await getHlsDiagnostics();
  } catch (e) {
    error.value = (e as Error).message;
  }
}

function speedText(r: HlsDiagnostics): string {
  if (r.speed.state === "running") return "Measuring…";
  if (r.speed.full_ladder_speed === null) return r.speed.error ? "Failed: " + r.speed.error : "Not measured";
  return `${r.speed.full_ladder_speed}× real time for all quality levels at 1080p (needs ${r.speed.target}×)`;
}
</script>

<template>
  <details class="diagnostics" @toggle="load">
    <summary>Video encoding on this machine</summary>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-else-if="!report">Checking…</p>
    <div v-else>
      <p v-for="w in report.warnings" :key="w" class="warning" role="alert">{{ w }}</p>
      <dl>
        <dt>Encoder</dt>
        <dd>{{ report.encoder.chosen }}<span v-if="report.encoder.low_power"> (low-power mode)</span> — setting: {{ report.encoder.setting }}</dd>
        <dt>Decoding</dt>
        <dd>{{ report.decode.on_gpu ? "on the GPU" : "on the CPU" }} — setting: {{ report.decode.setting }}</dd>
        <dt>GPU device</dt>
        <dd>{{ report.gpu_device.path }} — {{ report.gpu_device.present ? "present" : "not found" }}</dd>
        <dt>Speed</dt>
        <dd>{{ speedText(report) }}</dd>
        <dt>CPU cores</dt>
        <dd>{{ report.cpu_count }}</dd>
        <dt>ffmpeg</dt>
        <dd>{{ report.ffmpeg }}</dd>
        <dt>Hardware acceleration</dt>
        <dd>{{ report.hwaccels.join(", ") || "none" }}</dd>
      </dl>
      <h4>What was tried</h4>
      <ul>
        <li v-for="a in report.attempts" :key="a.what + a.detail">
          {{ a.worked ? "✓" : "✗" }} {{ a.what }}<span v-if="a.detail"> — {{ a.detail }}</span>
        </li>
        <li v-if="!report.attempts.length">Nothing yet: no video has been played since startup.</li>
      </ul>
    </div>
  </details>
</template>

<style>
.diagnostics { margin-top: var(--space-5); color: var(--text-dim); }
.diagnostics summary { cursor: pointer; color: var(--text); }
.diagnostics dl { display: grid; grid-template-columns: max-content 1fr; gap: var(--space-2) var(--space-4); }
.diagnostics dt { color: var(--text-dim); }
.diagnostics dd { margin: 0; color: var(--text); overflow-wrap: anywhere; }
.diagnostics .warning { color: var(--warn, #f5b942); }
</style>
