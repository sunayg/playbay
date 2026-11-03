<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { clearField, getSettings, resetField, saveCategory } from "../lib/api";
import type { FieldInfo, SettingsPayload } from "../types";
import CategoryNav from "./CategoryNav.vue";
import SettingField from "./SettingField.vue";
import VideoDiagnostics from "./VideoDiagnostics.vue";

const payload = ref<SettingsPayload | null>(null);
const selected = ref("");
const drafts = ref<Record<string, unknown>>({});
const errors = ref<Record<string, string>>({});
const message = ref("");
const loadError = ref("");
const saving = ref(false);

onMounted(async () => {
  try {
    payload.value = await getSettings();
    selected.value = payload.value.categories[0]?.id ?? "";
  } catch (e) {
    loadError.value = (e as Error).message;
  }
});

const fields = computed(() => (payload.value?.fields ?? []).filter((f) => f.category === selected.value));

function shown(field: FieldInfo): unknown {
  if (field.key in drafts.value) return drafts.value[field.key];
  return field.secret ? "" : field.value;
}

function select(id: string) {
  selected.value = id;
  errors.value = {};
  message.value = "";
}

function dropDrafts(keys: string[]) {
  for (const k of keys) delete drafts.value[k];
}

async function save() {
  const values: Record<string, unknown> = {};
  for (const f of fields.value) {
    if (!(f.key in drafts.value)) continue;
    if (f.secret && drafts.value[f.key] === "") continue;
    values[f.key] = drafts.value[f.key];
  }
  saving.value = true;
  message.value = "";
  try {
    const result = await saveCategory(selected.value, values);
    if (result.ok) {
      payload.value = result.payload;
      dropDrafts(fields.value.map((f) => f.key));
      errors.value = {};
      message.value = "Saved";
    } else {
      errors.value = result.errors;
    }
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    saving.value = false;
  }
}

async function act(key: string, call: (key: string) => Promise<SettingsPayload>) {
  try {
    payload.value = await call(key);
    dropDrafts([key]);
    delete errors.value[key];
  } catch (e) {
    errors.value = { ...errors.value, [key]: (e as Error).message };
  }
}
</script>

<template>
  <p v-if="loadError" class="error" role="alert">{{ loadError }}</p>
  <div v-else-if="payload" class="settings">
    <CategoryNav :categories="payload.categories" :selected="selected" @select="select" />
    <form class="category" @submit.prevent="save">
      <SettingField
        v-for="f in fields"
        :key="f.key"
        :field="f"
        :model-value="shown(f)"
        :error="errors[f.key]"
        @update:model-value="drafts[f.key] = $event"
        @reset="act(f.key, resetField)"
        @clear="act(f.key, clearField)"
      />
      <div class="save-row">
        <button type="submit" class="primary" :disabled="saving">Save</button>
        <span v-if="message" class="message" role="status">{{ message }}</span>
      </div>
      <VideoDiagnostics v-if="selected === 'streaming'" />
    </form>
  </div>
  <p v-else class="loading">Loading…</p>
</template>
