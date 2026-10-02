import { createRouter, createWebHistory } from "vue-router";

import DatasourcesView from "@/views/DatasourcesView.vue";
import ModelSettingsView from "@/views/ModelSettingsView.vue";
import WorkspaceView from "@/views/WorkspaceView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/workspace" },
    { path: "/workspace", component: WorkspaceView },
    { path: "/datasources", component: DatasourcesView },
    { path: "/settings/model", component: ModelSettingsView },
  ],
});
