import { computed, ref } from "vue";
import { defineStore } from "pinia";

import {
  clampConsoleWidth,
  clampLeftWidth,
  DEFAULT_WORKSPACE_LAYOUT,
  resolveWorkspaceLayout,
} from "@/lib/workspaceLayout";

export type ConsoleTab = "runs" | "overview" | "trace" | "outputs";

export const useWorkspaceStore = defineStore("workspace", () => {
  const selectedDatasourceId = ref<string | null>(null);
  const consoleTab = ref<ConsoleTab>("overview");
  const leftDrawerOpen = ref(false);
  const rightDrawerOpen = ref(false);
  const containerWidth = ref(0);
  const leftSidebarWidth = ref(DEFAULT_WORKSPACE_LAYOUT.leftDefaultWidth);
  const leftSidebarCollapsed = ref(false);
  const consoleWidth = ref(DEFAULT_WORKSPACE_LAYOUT.consoleDefaultWidth);
  const suppressedConsoleSessions = ref(new Set<string>());
  const effectiveLeftSidebarWidth = computed(() => leftSidebarCollapsed.value ? 52 : leftSidebarWidth.value);
  const layout = computed(() => resolveWorkspaceLayout({
    containerWidth: containerWidth.value,
    leftWidth: effectiveLeftSidebarWidth.value,
    consoleWidth: consoleWidth.value,
  }));
  const isNarrow = computed(() => layout.value.isNarrow);
  const consoleDockable = computed(() => layout.value.canDockConsole);
  const consoleMaxWidth = computed(() => layout.value.consoleMaxWidth);
  const hasSelectedDatasource = computed(() => selectedDatasourceId.value !== null);

  function setDatasource(datasourceId: string | null): void {
    selectedDatasourceId.value = datasourceId;
  }

  function setConsoleTab(tab: ConsoleTab): void {
    consoleTab.value = tab;
  }

  function setViewport(width: number): void {
    setContainerWidth(width);
  }

  function setContainerWidth(width: number): void {
    containerWidth.value = Math.max(0, width);
    consoleWidth.value = clampConsoleWidth(
      consoleWidth.value,
      containerWidth.value,
      effectiveLeftSidebarWidth.value,
    );
  }

  function setLeftSidebarWidth(width: number): void {
    leftSidebarWidth.value = clampLeftWidth(width);
    consoleWidth.value = clampConsoleWidth(
      consoleWidth.value,
      containerWidth.value,
      effectiveLeftSidebarWidth.value,
    );
  }

  function toggleLeftSidebar(): void {
    leftSidebarCollapsed.value = !leftSidebarCollapsed.value;
    consoleWidth.value = clampConsoleWidth(
      consoleWidth.value,
      containerWidth.value,
      effectiveLeftSidebarWidth.value,
    );
  }

  function setConsoleWidth(width: number): void {
    consoleWidth.value = clampConsoleWidth(
      width,
      containerWidth.value,
      effectiveLeftSidebarWidth.value,
    );
  }

  function isConsoleSuppressed(sessionId: string | null | undefined): boolean {
    return sessionId !== null && sessionId !== undefined && suppressedConsoleSessions.value.has(sessionId);
  }

  function suppressConsole(sessionId: string | null | undefined): void {
    if (!sessionId) return;
    suppressedConsoleSessions.value = new Set([...suppressedConsoleSessions.value, sessionId]);
  }

  function releaseConsole(sessionId: string | null | undefined): void {
    if (!sessionId || !suppressedConsoleSessions.value.has(sessionId)) return;
    const next = new Set(suppressedConsoleSessions.value);
    next.delete(sessionId);
    suppressedConsoleSessions.value = next;
  }

  return {
    selectedDatasourceId,
    consoleTab,
    leftDrawerOpen,
    rightDrawerOpen,
    containerWidth,
    leftSidebarWidth,
    leftSidebarCollapsed,
    effectiveLeftSidebarWidth,
    consoleWidth,
    consoleMaxWidth,
    consoleDockable,
    isNarrow,
    hasSelectedDatasource,
    setDatasource,
    setConsoleTab,
    setViewport,
    setContainerWidth,
    setLeftSidebarWidth,
    toggleLeftSidebar,
    setConsoleWidth,
    isConsoleSuppressed,
    suppressConsole,
    releaseConsole,
  };
});
