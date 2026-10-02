<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { RouterLink, RouterView } from "vue-router";
import { Database, LayoutPanelLeft, Moon, Settings2, Sun } from "@lucide/vue";

import { getHealth, type HealthResponse } from "@/api/client";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/lib/theme";

const health = ref<HealthResponse | null>(null);
const healthError = ref<string | null>(null);

const { isDark, toggleTheme } = useTheme();
const themeToggleLabel = computed(() => isDark.value ? "切换到白天主题" : "切换到黑夜主题");

const healthLabel = computed(() => {
  if (health.value) {
    return health.value.status;
  }
  if (healthError.value) {
    return "unavailable";
  }
  return "checking";
});

onMounted(async () => {
  try {
    health.value = await getHealth();
  } catch (error) {
    healthError.value = error instanceof Error ? error.message : "Health check failed";
  }
});
</script>

<template>
  <div class="app-frame">
    <header class="app-topbar">
      <RouterLink class="app-brand" to="/workspace" aria-label="DataPilot 工作台">
        <span class="app-brand-mark">DP</span>
        <span>DataPilot</span>
      </RouterLink>
      <nav class="app-nav" aria-label="主导航">
        <RouterLink to="/workspace"><LayoutPanelLeft :size="15" />工作台</RouterLink>
        <RouterLink to="/datasources"><Database :size="15" />数据源</RouterLink>
        <RouterLink to="/settings/model"><Settings2 :size="15" />模型设置</RouterLink>
      </nav>
      <div class="app-topbar-end">
        <Button variant="ghost" size="icon-sm" :aria-label="themeToggleLabel" @click="toggleTheme">
          <Sun v-if="isDark" :size="15" aria-hidden="true" />
          <Moon v-else :size="15" aria-hidden="true" />
        </Button>
        <span class="app-health">API {{ healthLabel }}</span>
      </div>
    </header>
    <main class="app-content"><RouterView /></main>
  </div>
</template>
