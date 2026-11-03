<script setup lang="ts">
import { useRouter } from "vue-router";
import SettingsPage from "../components/SettingsPage.vue";

// Full page load: until cutover "/" may be served by the legacy app.
const router = useRouter();
const home = (router.options.history.base || "") + "/";
</script>

<template>
  <header class="top">
    <a :href="home" class="back">&larr; PlayBay</a>
    <h1>Settings</h1>
  </header>
  <main>
    <SettingsPage />
  </main>
</template>
