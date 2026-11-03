<script setup lang="ts">
import type { TrackChoice } from "./useTextTracks";
import type { QualityLevel } from "./useHls";

defineProps<{
  qualityLevels?: QualityLevel[];
  // -1 is automatic.
  selectedQuality?: number;
  playingQuality?: number;
  audioChoices: TrackChoice[];
  activeAudio: number | null;
  captionChoices: TrackChoice[];
  activeCaption: number | null;
  // Seconds the subtitles are shifted later (negative: earlier).
  subtitleDelay?: number;
}>();
const emit = defineEmits<{
  audio: [index: number];
  caption: [index: number | null];
  quality: [index: number];
  // A step in seconds, or null to put the subtitles back on time.
  delay: [step: number | null];
}>();

const STEP = 0.5;
function delayText(seconds: number): string {
  return seconds === 0 ? "On time" : `${seconds > 0 ? "+" : "\u2212"}${Math.abs(seconds).toFixed(2).replace(/0$/, "")} s`;
}
</script>

<template>
  <div class="player-menu player-tracks" role="menu">
    <template v-if="qualityLevels && qualityLevels.length > 1">
      <p class="player-menu-label">Quality</p>
      <button type="button" role="menuitemradio" :aria-checked="selectedQuality === -1" :class="{ selected: selectedQuality === -1 }" @click="emit('quality', -1)">
        Auto<span v-if="selectedQuality === -1 && playingQuality !== undefined && playingQuality >= 0"> ({{ qualityLevels.find((l) => l.index === playingQuality)?.height }}p)</span>
      </button>
      <button
        v-for="level in qualityLevels"
        :key="'q-' + level.index"
        type="button"
        role="menuitemradio"
        :aria-checked="selectedQuality === level.index"
        :class="{ selected: selectedQuality === level.index }"
        @click="emit('quality', level.index)"
      >{{ level.height }}p</button>
    </template>
    <p class="player-menu-label">Audio</p>
    <button
      v-for="track in audioChoices"
      :key="'aud-' + track.index"
      type="button"
      role="menuitemradio"
      :aria-checked="activeAudio === track.index"
      :class="{ selected: activeAudio === track.index }"
      @click="emit('audio', track.index)"
    >{{ track.label }}</button>
    <button v-if="!audioChoices.length" type="button" class="selected" role="menuitemradio" aria-checked="true">Original</button>
    <p class="player-menu-label">Subtitles</p>
    <button type="button" role="menuitemradio" :aria-checked="activeCaption === null" :class="{ selected: activeCaption === null }" @click="emit('caption', null)">Off</button>
    <button
      v-for="track in captionChoices"
      :key="'cap-' + track.index"
      type="button"
      role="menuitemradio"
      :aria-checked="activeCaption === track.index"
      :class="{ selected: activeCaption === track.index }"
      @click="emit('caption', track.index)"
    >{{ track.label }}</button>
    <p v-if="!captionChoices.length" class="player-menu-empty">No subtitles</p>
    <template v-if="captionChoices.length && activeCaption !== null && subtitleDelay !== undefined">
      <p class="player-menu-label">Subtitle timing</p>
      <div class="player-delay" role="group" aria-label="Subtitle timing">
        <button type="button" aria-label="Show subtitles earlier" title="Earlier ( [ )" @click="emit('delay', -STEP)">&minus;</button>
        <button type="button" class="player-delay-value" aria-label="Put subtitles back on time" title="Reset" @click="emit('delay', null)">{{ delayText(subtitleDelay) }}</button>
        <button type="button" aria-label="Show subtitles later" title="Later ( ] )" @click="emit('delay', STEP)">+</button>
      </div>
    </template>
  </div>
</template>
