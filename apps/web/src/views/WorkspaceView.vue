<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  ArrowUp,
  Database,
  FolderOpen,
  FolderKanban,
  List,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  ShieldCheck,
  Square,
  Trash2,
  X,
} from "@lucide/vue";

import { listSessions } from "@/api/sessions";
import type { Message, Run, RunArtifact, RunStatus, Session, SqlAudit, ToolCall } from "@/api/types";
import ArtifactViewer from "@/components/ArtifactViewer.vue";
import CatalogOverlay, { type CatalogOverlayItem } from "@/components/CatalogOverlay.vue";
import RichMarkdown from "@/components/RichMarkdown.vue";
import RunActivityCard from "@/components/RunActivityCard.vue";
import WorkspaceConsole from "@/components/WorkspaceConsole.vue";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { incompleteHint } from "@/lib/incompleteHint";
import { deriveRunActivity, type RunActivity, type RunActivityItem } from "@/lib/runActivity";
import { answerProvenanceLabel, chartArtifactsForAnswer } from "@/lib/answerPresentation";
import { runForActivityAnchor } from "@/lib/runMessageBinding";
import { deriveRunTrace } from "@/lib/runTrace";
import {
  daySeparatorLabel,
  localDayKey,
  messageClockLabel,
  relativeActivityLabel,
} from "@/lib/workspaceTime";
import { useDatasourceStore } from "@/stores/datasourceStore";
import { useModelStore } from "@/stores/modelStore";
import { useRunStore } from "@/stores/runStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const sessions = useSessionStore();
const datasources = useDatasourceStore();
const models = useModelStore();
const runs = useRunStore();
const workspace = useWorkspaceStore();

const OVERLAY_PAGE_SIZE = 20;
const SESSION_DELETE_COPY =
  "删除后无法恢复。该会话的全部 Run、消息、历史回答、产物和工作区都会被永久删除。";

const question = ref("");
const newSessionOpen = ref(false);
const newDatasourceId = ref("");
const submitting = ref(false);
const actionError = ref<string | null>(null);
const selectedArtifactId = ref("");
const selectedDetailId = ref("");
const detailOpen = ref(false);
const workspaceContainer = ref<HTMLElement | null>(null);
const timeline = ref<HTMLElement | null>(null);
const shouldFollowTimeline = ref(true);
const sessionOverlayOpen = ref(false);
const sessionOverlayDraft = ref("");
const sessionOverlayQuery = ref("");
const sessionOverlayPage = ref(1);
const sessionOverlayItems = ref<Session[]>([]);
const sessionOverlayTotal = ref(0);
const sessionOverlayLoading = ref(false);
const pendingDeleteSessionId = ref<string | null>(null);
const deletingSession = ref(false);
const restoreSessionOverlay = ref(false);
let refreshedTerminalRunId = "";
let workspaceResizeObserver: ResizeObserver | null = null;
let resizeTarget: "left" | "console" | null = null;
let resizeStartX = 0;
let resizeStartWidth = 0;
let suppressNextDrawerClose = false;
let sessionSearchTimer = 0;
let loadingOlderMessages = false;

const readyDatasources = computed(() =>
  datasources.items.filter(
    (item) =>
      (item.status === "schema_ready" || item.status === "ready") && item.mask_fields_confirmed,
  ),
);
const currentDatasource = computed(() =>
  datasources.items.find((item) => item.id === sessions.currentSession?.selected_datasource_id) ?? null,
);
const activeModel = computed(() => models.items.find((item) => item.is_active) ?? null);
const canAsk = computed(
  () => sessions.currentSession !== null && currentDatasource.value !== null && activeModel.value !== null && !runs.isActive,
);
const latestRun = computed(() => sessions.runs[0] ?? null);
const activity = computed(() => {
  if (runs.currentRun === null) return null;
  return deriveRunActivity({
    run: runs.currentRun,
    events: runs.projection?.eventLog ?? [],
    toolCalls: runs.toolCalls,
    sqlAudits: runs.sqlAudits,
    artifacts: runs.artifacts,
  });
});
type TimelineActivityCard = {
  activity: RunActivity;
  toolCalls: readonly ToolCall[];
  sqlAudits: readonly SqlAudit[];
  artifacts: readonly RunArtifact[];
};
const activityByUserMessageId = computed(() => {
  const cards = new Map<string, TimelineActivityCard>();
  const candidates: Run[] = [];
  if (runs.currentRun !== null) candidates.push(runs.currentRun);
  for (const run of sessions.runs) {
    if (run.id !== runs.currentRun?.id) candidates.push(run);
  }
  for (const run of candidates) {
    const userMessageId = run.user_message_id;
    if (userMessageId === null) continue;
    const facts = run.id === runs.currentRun?.id
      ? {
          run: runs.currentRun,
          events: runs.projection?.eventLog ?? [],
          toolCalls: runs.toolCalls,
          sqlAudits: runs.sqlAudits,
          artifacts: runs.artifacts,
        }
      : runs.sessionActivityFacts?.[run.id];
    if (facts === undefined) continue;
    cards.set(userMessageId, {
      activity: deriveRunActivity(facts),
      toolCalls: facts.toolCalls,
      sqlAudits: facts.sqlAudits,
      artifacts: facts.artifacts,
    });
  }
  return cards;
});
const traceEntries = computed(() => runs.currentRun === null
  ? []
  : deriveRunTrace({
      run: runs.currentRun,
      events: runs.projection?.eventLog ?? [],
      toolCalls: runs.toolCalls,
      sqlAudits: runs.sqlAudits,
      artifacts: runs.artifacts,
    }));
const currentAnswerMessage = computed(() => {
  const runId = runs.currentRun?.id;
  if (runId === undefined) return null;
  for (let index = sessions.messages.length - 1; index >= 0; index -= 1) {
    const message = sessions.messages[index];
    if (message?.role === "assistant" && message.run_id === runId) return message;
  }
  return null;
});
const answerDraftVisible = computed(() => {
  const run = runs.currentRun;
  return run !== null
    && (run.status === "queued" || run.status === "running")
    && currentAnswerMessage.value === null
    && Boolean(runs.projection?.answerDraft);
});
const currentAnswerEvidenceRefs = computed(() => currentAnswerMessage.value?.answer_evidence_refs ?? []);
const sessionOverlayCatalog = computed<CatalogOverlayItem[]>(() =>
  sessionOverlayItems.value.map((item) => ({
    id: item.id,
    title: item.title,
    meta: relativeActivityLabel(item.last_message_at),
    canDelete: true,
  })),
);
const pendingDeleteTitle = computed(() => {
  const id = pendingDeleteSessionId.value;
  if (id === null) return "";
  return (
    sessions.sessions.find((item) => item.id === id)?.title
    ?? sessionOverlayItems.value.find((item) => item.id === id)?.title
    ?? "该会话"
  );
});
const composerHint = computed(() => {
  if (actionError.value || runs.error || sessions.error) return actionError.value ?? runs.error ?? sessions.error;
  if (currentDatasource.value === null) return "当前会话没有可分析的数据源。";
  if (activeModel.value === null) return "还没有激活模型配置。";
  if (runs.isActive) return runs.cancelRequested ? "取消请求已发送，正在等待本次分析收尾。" : "当前分析正在进行，可取消后再提新问题。";
  return null;
});

function runStatusClass(
  status: RunStatus | null | undefined,
  completionKind: "completed" | "partial" | "clarification" | null = null,
): string {
  if (status === "succeeded" && completionKind === "partial") return "status-warning";
  if (status === "succeeded" && completionKind === "clarification") return "status-warning";
  if (status === "succeeded") return "status-complete";
  if (status === "failed" || status === "canceled") return "status-error";
  if (status === "queued" || status === "running") return "status-running";
  return "status-muted";
}

function runStatusLabel(
  status: RunStatus | null | undefined,
  completionKind: "completed" | "partial" | "clarification" | null = null,
): string {
  const labels: Partial<Record<RunStatus, string>> = {
    queued: "等待中",
    running: "运行中",
    succeeded: completionKind === "partial"
      ? "部分完成"
      : completionKind === "clarification"
        ? "需要补充信息"
        : "已完成",
    failed: "未完成",
    canceled: "已取消",
  };
  return status ? labels[status] ?? "未开始" : "未开始";
}

function makeIdempotencyKey(): string {
  const id = globalThis.crypto?.randomUUID?.();
  return id ? `web-${id}` : `web-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function activityCard(message: Message): TimelineActivityCard | null {
  const run = runForActivityAnchor(message, runs.currentRun, sessions.runs);
  if (run?.user_message_id == null) return null;
  return activityByUserMessageId.value.get(run.user_message_id) ?? null;
}

function runForMessage(message: Message): Run | null {
  if (message.run_id === null) return null;
  if (runs.currentRun?.id === message.run_id) return runs.currentRun;
  return sessions.runs.find((run) => run.id === message.run_id) ?? null;
}

function outcomeLabel(message: Message): string {
  const messageRun = runForMessage(message);
  if (messageRun?.status === "failed") return "运行未完成";
  if (messageRun?.status === "canceled") return "运行已取消";
  if (messageRun?.completion_kind === "clarification") return "需要补充信息";
  if (messageRun?.completion_kind === "partial") return "部分完成说明";
  return "结果说明";
}

function answerProvenanceForMessage(message: Message): string | null {
  return answerProvenanceLabel(runForMessage(message));
}

function artifactsForRun(runId: string | null): readonly RunArtifact[] {
  if (runId === null) return [];
  if (runs.currentRun?.id === runId) return runs.artifacts;
  return runs.sessionActivityFacts[runId]?.artifacts ?? [];
}

function answerCharts(message: Message): RunArtifact[] {
  return chartArtifactsForAnswer(message, artifactsForRun(message.run_id));
}

function answerAnchorPrefix(message: Message): string {
  return `answer-${message.id}`;
}

function openAnswerEvidence(message: Message): void {
  if (message.run_id !== runs.currentRun?.id || !message.answer_evidence_refs?.length) return;
  workspace.setConsoleTab("overview");
  openDetail();
}

async function navigateToAnswer(anchorId: string): Promise<void> {
  if (workspace.isNarrow) closeDrawerSilently();
  await nextTick();
  const heading = document.getElementById(anchorId);
  if (heading === null) return;
  heading.scrollIntoView({ behavior: "smooth", block: "start" });
  heading.focus({ preventScroll: true });
}

function openDetail({ automatic = false }: { automatic?: boolean } = {}): void {
  const sessionId = sessions.currentSession?.id;
  if (automatic && workspace.isConsoleSuppressed(sessionId)) return;
  if (!automatic) workspace.releaseConsole(sessionId);
  if (workspace.isNarrow) {
    if (!automatic) workspace.rightDrawerOpen = true;
    return;
  }
  detailOpen.value = true;
}

function closeDetail({ rememberPreference = true }: { rememberPreference?: boolean } = {}): void {
  detailOpen.value = false;
  workspace.rightDrawerOpen = false;
  if (rememberPreference) workspace.suppressConsole(sessions.currentSession?.id);
  selectedArtifactId.value = "";
  selectedDetailId.value = "";
  workspace.setConsoleTab("overview");
}

function closeDrawerSilently(): void {
  if (!workspace.rightDrawerOpen) return;
  suppressNextDrawerClose = true;
  workspace.rightDrawerOpen = false;
}

function handleRightDrawerOpen(open: boolean): void {
  workspace.rightDrawerOpen = open;
  if (!open) {
    const rememberPreference = !suppressNextDrawerClose;
    suppressNextDrawerClose = false;
    closeDetail({ rememberPreference });
  }
}

function selectArtifact(artifactId: string): void {
  selectedArtifactId.value = artifactId;
  selectedDetailId.value = `artifact:${artifactId}`;
  openDetail();
}

function openArtifacts(): void {
  workspace.setConsoleTab("outputs");
  openDetail();
}

async function openActivityDetail(runId: string, item: RunActivityItem): Promise<void> {
  if (runs.currentRun?.id !== runId) await selectRun(runId);
  if (item.artifactId !== null) {
    selectArtifact(item.artifactId);
    return;
  }
  selectedDetailId.value = item.id;
  openDetail();
}

async function selectArtifactFromActivity(runId: string, artifactId: string): Promise<void> {
  if (runs.currentRun?.id !== runId) await selectRun(runId);
  selectArtifact(artifactId);
}

function closeInspection(): void {
  selectedArtifactId.value = "";
  selectedDetailId.value = "";
}

function selectInspectorDetail(detailId: string): void {
  selectedDetailId.value = detailId;
  openDetail();
}

async function selectRun(runId: string): Promise<void> {
  selectedArtifactId.value = "";
  selectedDetailId.value = "";
  workspace.setConsoleTab("overview");
  await runs.loadHistory(runId);
  openDetail();
}

async function syncWorkspaceAfterSessionChange(): Promise<void> {
  if (sessions.currentSession === null) {
    runs.clear();
    workspace.setDatasource(null);
    selectedArtifactId.value = "";
    selectedDetailId.value = "";
    workspace.setConsoleTab("overview");
    detailOpen.value = false;
    closeDrawerSilently();
    return;
  }
  workspace.setDatasource(sessions.currentSession.selected_datasource_id ?? null);
  selectedArtifactId.value = "";
  selectedDetailId.value = "";
  workspace.setConsoleTab("overview");
  detailOpen.value = false;
  closeDrawerSilently();
  shouldFollowTimeline.value = true;
  if (latestRun.value !== null) {
    runs.clearSessionActivityFacts();
    await runs.loadHistory(latestRun.value.id);
    openDetail({ automatic: true });
  } else {
    runs.clear();
  }
  await runs.hydrateSessionRuns(sessions.runs);
  workspace.leftDrawerOpen = false;
}

async function selectSession(sessionId: string): Promise<void> {
  actionError.value = null;
  runs.stopStream();
  await sessions.select(sessionId);
  await syncWorkspaceAfterSessionChange();
}

async function refreshSessionOverlay(): Promise<void> {
  sessionOverlayLoading.value = true;
  try {
    const page = await listSessions(
      sessionOverlayPage.value,
      OVERLAY_PAGE_SIZE,
      sessionOverlayQuery.value,
    );
    sessionOverlayItems.value = page.items;
    sessionOverlayTotal.value = page.total;
    const pageCount = Math.max(1, Math.ceil(page.total / OVERLAY_PAGE_SIZE) || 1);
    if (sessionOverlayPage.value > pageCount) {
      sessionOverlayPage.value = pageCount;
      if (page.total > 0) await refreshSessionOverlay();
    }
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : "会话列表读取失败";
  } finally {
    sessionOverlayLoading.value = false;
  }
}

function openSessionOverlay(): void {
  sessionOverlayDraft.value = "";
  sessionOverlayQuery.value = "";
  sessionOverlayPage.value = 1;
  sessionOverlayOpen.value = true;
  void refreshSessionOverlay();
}

function onSessionOverlayQuery(query: string): void {
  sessionOverlayDraft.value = query;
  window.clearTimeout(sessionSearchTimer);
  sessionSearchTimer = window.setTimeout(() => {
    sessionOverlayQuery.value = query;
    sessionOverlayPage.value = 1;
    void refreshSessionOverlay();
  }, 250);
}

function onSessionOverlayPage(page: number): void {
  sessionOverlayPage.value = page;
  void refreshSessionOverlay();
}

function onDeleteDialogOpen(open: boolean): void {
  if (!open) cancelDeleteSession();
}

async function selectOverlaySession(sessionId: string): Promise<void> {
  sessionOverlayOpen.value = false;
  await selectSession(sessionId);
}

function requestDeleteSession(sessionId: string): void {
  restoreSessionOverlay.value = sessionOverlayOpen.value;
  sessionOverlayOpen.value = false;
  pendingDeleteSessionId.value = sessionId;
}

function cancelDeleteSession(): void {
  if (deletingSession.value) return;
  pendingDeleteSessionId.value = null;
  if (restoreSessionOverlay.value) {
    sessionOverlayOpen.value = true;
    restoreSessionOverlay.value = false;
  }
}

async function confirmDeleteSession(): Promise<void> {
  const sessionId = pendingDeleteSessionId.value;
  if (sessionId === null) return;
  const wasCurrent = sessions.currentSession?.id === sessionId;
  deletingSession.value = true;
  actionError.value = null;
  try {
    if (wasCurrent) runs.stopStream();
    await sessions.remove(sessionId);
    pendingDeleteSessionId.value = null;
    if (wasCurrent) await syncWorkspaceAfterSessionChange();
    if (restoreSessionOverlay.value) {
      sessionOverlayOpen.value = true;
      restoreSessionOverlay.value = false;
      await refreshSessionOverlay();
    }
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : "删除会话失败";
  } finally {
    deletingSession.value = false;
  }
}

function showDaySeparator(index: number): boolean {
  const message = sessions.messages[index];
  if (message === undefined) return false;
  const previous = sessions.messages[index - 1];
  if (previous === undefined) return true;
  return localDayKey(previous.created_at) !== localDayKey(message.created_at);
}

function openNewSession(): void {
  newDatasourceId.value = readyDatasources.value[0]?.id ?? "";
  newSessionOpen.value = true;
}

async function createNewSession(): Promise<void> {
  if (!newDatasourceId.value) return;
  actionError.value = null;
  try {
    const session = await sessions.create("新分析", newDatasourceId.value);
    workspace.setDatasource(session.selected_datasource_id);
    runs.clear();
    selectedArtifactId.value = "";
    selectedDetailId.value = "";
    detailOpen.value = false;
    closeDrawerSilently();
    workspace.setConsoleTab("overview");
    newSessionOpen.value = false;
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : "新建会话失败";
  }
}

async function submitQuestion(): Promise<void> {
  const text = question.value.trim();
  if (!text || !canAsk.value) return;
  submitting.value = true;
  actionError.value = null;
  try {
    const runId = await sessions.submitRun(text, makeIdempotencyKey());
    question.value = "";
    await sessions.select(sessions.currentSession?.id ?? "");
    await runs.begin(runId);
    openDetail({ automatic: true });
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : "提交问题失败";
  } finally {
    submitting.value = false;
  }
}

async function cancelCurrentRun(): Promise<void> {
  if (runs.currentRun === null || runs.cancelRequested) return;
  try {
    await runs.requestCancel(runs.currentRun.id);
  } catch (caught) {
    actionError.value = caught instanceof Error ? caught.message : "取消分析失败";
  }
}

function onTimelineScroll(): void {
  const element = timeline.value;
  if (element === null) return;
  shouldFollowTimeline.value = element.scrollHeight - element.scrollTop - element.clientHeight < 36;
  if (element.scrollTop < 48) void loadOlderTimeline();
}

async function loadOlderTimeline(): Promise<void> {
  if (loadingOlderMessages) return;
  const element = timeline.value;
  if (element === null) return;
  const previousHeight = element.scrollHeight;
  const previousTop = element.scrollTop;
  loadingOlderMessages = true;
  try {
    const loaded = await sessions.loadOlderMessages();
    if (!loaded) return;
    await nextTick();
    element.scrollTop = previousTop + (element.scrollHeight - previousHeight);
  } finally {
    loadingOlderMessages = false;
  }
}

async function followTimeline(): Promise<void> {
  if (!shouldFollowTimeline.value) return;
  await nextTick();
  const element = timeline.value;
  if (element !== null) element.scrollTop = element.scrollHeight;
}

function syncViewport(): void {
  workspace.setContainerWidth(workspaceContainer.value?.clientWidth ?? window.innerWidth);
}

function startResize(target: "left" | "console", event: PointerEvent): void {
  if (workspace.isNarrow) return;
  resizeTarget = target;
  resizeStartX = event.clientX;
  resizeStartWidth = target === "left" ? workspace.leftSidebarWidth : workspace.consoleWidth;
  document.body.classList.add("workspace-resizing");
  window.addEventListener("pointermove", resizeWorkspace);
  window.addEventListener("pointerup", stopResize, { once: true });
  event.preventDefault();
}

function resizeWorkspace(event: PointerEvent): void {
  if (resizeTarget === "left") {
    workspace.setLeftSidebarWidth(resizeStartWidth + event.clientX - resizeStartX);
    return;
  }
  if (resizeTarget === "console") workspace.setConsoleWidth(resizeStartWidth - (event.clientX - resizeStartX));
}

function stopResize(): void {
  resizeTarget = null;
  document.body.classList.remove("workspace-resizing");
  window.removeEventListener("pointermove", resizeWorkspace);
}

function adjustLeftWidth(amount: number): void {
  workspace.setLeftSidebarWidth(workspace.leftSidebarWidth + amount);
}

function adjustConsoleWidth(amount: number): void {
  workspace.setConsoleWidth(workspace.consoleWidth + amount);
}

onMounted(async () => {
  syncViewport();
  workspaceResizeObserver = new ResizeObserver(syncViewport);
  if (workspaceContainer.value !== null) workspaceResizeObserver.observe(workspaceContainer.value);
  await Promise.all([sessions.load(), datasources.load(), models.load()]);
  if (sessions.sessions[0]) await selectSession(sessions.sessions[0].id);
});

onBeforeUnmount(() => {
  runs.stopStream();
  workspaceResizeObserver?.disconnect();
  stopResize();
  window.clearTimeout(sessionSearchTimer);
});

watch(
  () => runs.projection?.terminal,
  async (terminal) => {
    const runId = runs.currentRun?.id;
    if (!terminal || !runId || refreshedTerminalRunId === runId) return;
    refreshedTerminalRunId = runId;
    if (sessions.currentSession !== null) {
      await sessions.select(sessions.currentSession.id);
      await runs.hydrateSessionRuns(sessions.runs);
    }
  },
);

watch(
  () => runs.projection?.answerMessageId,
  async (messageId) => {
    const sessionId = sessions.currentSession?.id;
    if (!messageId || !sessionId) return;
    if (!sessions.messages.some((message) => message.id === messageId)) {
      await sessions.select(sessionId);
    }
    if (sessions.messages.some((message) => message.id === messageId)) {
      runs.clearAnswerDraft();
    }
  },
);

watch(
  () => sessions.currentSession?.selected_datasource_id,
  (datasourceId) => workspace.setDatasource(datasourceId ?? null),
);

watch(
  () => runs.currentRun?.id,
  (runId, previousRunId) => {
    if (runId === previousRunId) return;
    selectedArtifactId.value = "";
    selectedDetailId.value = "";
    workspace.setConsoleTab("overview");
    shouldFollowTimeline.value = true;
  },
);

watch(
  () => workspace.consoleDockable,
  (dockable, wasDockable) => {
    if (dockable === wasDockable) return;
    if (!dockable && detailOpen.value) {
      detailOpen.value = false;
      if (!workspace.isConsoleSuppressed(sessions.currentSession?.id)) workspace.rightDrawerOpen = true;
      return;
    }
    if (dockable && workspace.rightDrawerOpen) {
      closeDrawerSilently();
      if (!workspace.isConsoleSuppressed(sessions.currentSession?.id)) detailOpen.value = true;
    }
  },
);

watch(
  () => runs.artifacts,
  (artifacts) => {
    if (selectedArtifactId.value && !artifacts.some((artifact) => artifact.id === selectedArtifactId.value)) {
      selectedArtifactId.value = "";
      if (selectedDetailId.value.startsWith("artifact:")) selectedDetailId.value = "";
    }
  },
  { immediate: true },
);

watch(
  [
    () => sessions.messages.length,
    () => runs.projection?.lastSeq,
    () => runs.currentRun?.id,
  ],
  () => { void followTimeline(); },
);
</script>

<template>
  <section
    ref="workspaceContainer"
    class="workspace"
    :class="{ 'workspace-narrow': workspace.isNarrow }"
    aria-label="数据分析工作台"
  >
    <div class="workspace-mobile-tools">
      <Button variant="outline" size="icon-sm" title="打开会话与数据源" @click="workspace.leftDrawerOpen = true">
        <PanelLeftOpen :size="16" aria-hidden="true" /><span class="sr-only">打开会话与数据源</span>
      </Button>
      <span class="workspace-mobile-title">{{ sessions.currentSession?.title ?? "工作台" }}</span>
      <Button variant="outline" size="icon-sm" title="打开运行详情" @click="openDetail()">
        <Square :size="15" aria-hidden="true" /><span class="sr-only">打开运行详情</span>
      </Button>
    </div>

    <div
      class="workspace-grid"
      :class="{ 'has-detail': detailOpen }"
      :style="{
        '--workspace-left-width': `${workspace.effectiveLeftSidebarWidth}px`,
        '--workspace-console-width': `${workspace.consoleWidth}px`,
      }"
    >
      <aside class="workspace-left" :class="{ 'is-collapsed': workspace.leftSidebarCollapsed }">
        <div class="workspace-side-content">
          <div class="side-heading">
            <div class="side-heading-title">
              <span>会话</span>
            </div>
            <div class="side-heading-actions">
              <Button variant="ghost" size="icon-sm" title="全部会话" @click="openSessionOverlay">
                <List :size="16" aria-hidden="true" /><span class="sr-only">全部会话</span>
              </Button>
              <Button variant="ghost" size="icon-sm" title="收起会话栏" @click="workspace.toggleLeftSidebar">
                <PanelLeftClose :size="16" aria-hidden="true" /><span class="sr-only">收起会话栏</span>
              </Button>
              <Button variant="ghost" size="icon-sm" title="新建会话" @click="openNewSession">
                <Plus :size="16" aria-hidden="true" /><span class="sr-only">新建会话</span>
              </Button>
            </div>
          </div>
          <div class="session-list">
            <div
              v-for="session in sessions.sessions"
              :key="session.id"
              class="session-row"
              :class="{ active: session.id === sessions.currentSession?.id }"
            >
              <button type="button" class="session-row-main" @click="selectSession(session.id)">
                <span>{{ session.title }}</span>
                <small>{{ relativeActivityLabel(session.last_message_at) }}</small>
              </button>
              <Button
                variant="ghost"
                size="icon-sm"
                title="删除会话"
                @click="requestDeleteSession(session.id)"
              >
                <Trash2 :size="14" aria-hidden="true" /><span class="sr-only">删除会话</span>
              </Button>
            </div>
            <p v-if="!sessions.loading && !sessions.hasSessions" class="side-empty">还没有分析会话。</p>
          </div>

          <div class="side-section">
            <div class="side-heading"><span>当前数据源</span><Database :size="14" aria-hidden="true" /></div>
            <template v-if="currentDatasource">
              <strong>{{ currentDatasource.name }}</strong>
              <span class="side-meta">{{ currentDatasource.type.toUpperCase() }} / r{{ currentDatasource.schema_revision }}</span>
            </template>
            <p v-else class="side-empty">新建会话时选择已就绪数据源。</p>
          </div>

        </div>
        <Button
          v-if="workspace.leftSidebarCollapsed"
          class="workspace-left-expand"
          variant="ghost"
          size="icon-sm"
          title="展开会话栏"
          @click="workspace.toggleLeftSidebar"
        >
          <PanelLeftOpen :size="16" aria-hidden="true" /><span class="sr-only">展开会话栏</span>
        </Button>
        <button
          class="workspace-resize-handle workspace-left-resize-handle"
          type="button"
          aria-label="调整会话栏宽度"
          @pointerdown="startResize('left', $event)"
          @keydown.left.prevent="adjustLeftWidth(-12)"
          @keydown.right.prevent="adjustLeftWidth(12)"
        />
      </aside>

      <main class="workspace-main">
        <div v-if="sessions.currentSession === null" class="workspace-empty">
          <FolderKanban :size="30" stroke-width="1.4" aria-hidden="true" />
          <h1>从一个数据源开始</h1>
          <p>先选择已完成准备的数据源，再新建分析会话。</p>
          <Button :disabled="readyDatasources.length === 0" @click="openNewSession"><Plus :size="16" aria-hidden="true" />新建会话</Button>
        </div>

        <template v-else>
          <header class="conversation-header">
            <div>
              <h1>{{ sessions.currentSession.title }}</h1>
              <p>{{ currentDatasource?.name ?? "未选择数据源" }}</p>
            </div>
            <div class="conversation-header-actions">
              <Badge variant="outline" :class="runStatusClass(runs.currentRun?.status, runs.currentRun?.completion_kind)">
                {{ runStatusLabel(runs.currentRun?.status, runs.currentRun?.completion_kind) }}
              </Badge>
              <Button
                v-if="!workspace.isNarrow"
                variant="ghost"
                size="icon-sm"
                :title="detailOpen ? '关闭运行详情' : '打开运行详情'"
                @click="detailOpen ? closeDetail() : openDetail()"
              >
                <PanelRightClose v-if="detailOpen" :size="16" aria-hidden="true" />
                <PanelRightOpen v-else :size="16" aria-hidden="true" />
                <span class="sr-only">{{ detailOpen ? "关闭运行详情" : "打开运行详情" }}</span>
              </Button>
            </div>
          </header>

          <div ref="timeline" class="message-timeline" @scroll.passive="onTimelineScroll">
            <template v-for="(message, index) in sessions.messages" :key="message.id">
              <div v-if="showDaySeparator(index)" class="day-separator">{{ daySeparatorLabel(message.created_at) }}</div>
              <article class="message" :class="message.role">
                <div class="message-role">
                  <span v-if="message.role === 'assistant'" class="message-role-dot" aria-hidden="true" />
                  {{ message.role === "user" ? "你" : "DataPilot" }}<template v-if="messageClockLabel(message.created_at)"> · {{ messageClockLabel(message.created_at) }}</template>
                </div>
                <RichMarkdown
                  v-if="message.role === 'assistant' && runForMessage(message)?.status !== 'failed' && runForMessage(message)?.status !== 'canceled'"
                  :content="message.content_text"
                  :anchor-prefix="answerAnchorPrefix(message)"
                  answer
                />
                <section v-else-if="message.role === 'assistant'" class="message-status-copy">
                  <strong>{{ outcomeLabel(message) }}</strong>
                  <p>{{ message.content_text }}</p>
                </section>
                <p v-else>{{ message.content_text }}</p>
                <div
                  v-if="message.role === 'assistant' && answerCharts(message).length"
                  class="answer-charts"
                >
                  <ArtifactViewer
                    v-for="artifact in answerCharts(message)"
                    :key="artifact.id"
                    :artifact="artifact"
                    hide-download
                  />
                </div>
                <p
                  v-if="message.role === 'assistant' && answerProvenanceForMessage(message)"
                  class="answer-provenance"
                >
                  {{ answerProvenanceForMessage(message) }}
                </p>
                <div
                  v-if="(message.role === 'assistant' && message.run_id === activity?.runId && activity.artifactCount) || (message.role === 'assistant' && message.run_id === runs.currentRun?.id && message.answer_evidence_refs?.length)"
                  class="message-actions"
                >
                  <Button
                    v-if="message.role === 'assistant' && message.run_id === activity?.runId && activity.artifactCount"
                    class="message-action message-artifact-link"
                    variant="outline"
                    size="sm"
                    @click="openArtifacts"
                  >
                    <FolderOpen class="message-action-icon" :size="14" aria-hidden="true" />
                    <span>查看本次产物</span>
                    <span class="message-action-count">{{ activity.artifactCount }} 项</span>
                  </Button>
                  <Button
                    v-if="message.role === 'assistant' && message.run_id === runs.currentRun?.id && message.answer_evidence_refs?.length"
                    class="message-action message-evidence-link"
                    variant="outline"
                    size="sm"
                    @click="openAnswerEvidence(message)"
                  >
                    <ShieldCheck class="message-action-icon" :size="14" aria-hidden="true" />
                    <span>查看本答案依据</span>
                    <span class="message-action-count">{{ message.answer_evidence_refs.length }} 项</span>
                  </Button>
                </div>
              </article>
              <RunActivityCard
                v-if="activityCard(message)"
                :activity="activityCard(message)!.activity"
                :tool-calls="activityCard(message)!.toolCalls"
                :sql-audits="activityCard(message)!.sqlAudits"
                :artifacts="activityCard(message)!.artifacts"
                @open-detail="openActivityDetail(activityCard(message)!.activity.runId, $event)"
                @open-artifact="selectArtifactFromActivity(activityCard(message)!.activity.runId, $event)"
              />
              <p
                v-if="message.role === 'assistant' && message.run_id === activity?.runId && runs.currentRun?.completion_kind === 'partial'"
                class="partial-answer"
              >
                本次回答不完整：{{ incompleteHint(runs.currentRun.incomplete_reason) }}
              </p>
            </template>
            <article v-if="answerDraftVisible" class="message assistant message-streaming">
              <div class="message-role">
                <span class="message-role-dot" aria-hidden="true" />
                DataPilot
              </div>
              <RichMarkdown
                :content="runs.projection?.answerDraft ?? ''"
                answer
                streaming
                anchor-prefix="answer-streaming"
              />
            </article>
            <div v-if="sessions.messages.length === 0" class="message-empty">提出一个和当前数据源有关的问题，结果会在这里保留。</div>
          </div>

          <footer class="question-box">
            <div class="composer-context">
              <span>{{ currentDatasource?.name ?? "未选择数据源" }}</span>
              <span v-if="runs.isActive">{{ runs.cancelRequested ? "正在取消" : "分析进行中" }}</span>
              <span v-else-if="activeModel">{{ activeModel.name }}</span>
            </div>
            <p v-if="composerHint" class="question-hint" :class="{ 'question-error': actionError || runs.error || sessions.error }">{{ composerHint }}</p>
            <Textarea
              v-model="question"
              :disabled="!canAsk || submitting"
              placeholder="输入分析问题"
              @keydown.meta.enter.prevent="submitQuestion"
              @keydown.ctrl.enter.prevent="submitQuestion"
            />
            <div class="question-actions">
              <span>{{ canAsk ? "本次分析只使用当前数据源。" : "" }}</span>
              <Button
                v-if="runs.isActive"
                variant="outline"
                :disabled="runs.cancelRequested"
                @click="cancelCurrentRun"
              >
                <Square :size="14" aria-hidden="true" />取消分析
              </Button>
              <Button v-else :disabled="!question.trim() || !canAsk || submitting" @click="submitQuestion">
                <ArrowUp :size="16" aria-hidden="true" />发送
              </Button>
            </div>
          </footer>
        </template>
      </main>

      <aside v-if="detailOpen" class="workspace-right">
        <button
          class="workspace-resize-handle workspace-console-resize-handle"
          type="button"
          aria-label="调整运行检查台宽度"
          @pointerdown="startResize('console', $event)"
          @keydown.left.prevent="adjustConsoleWidth(12)"
          @keydown.right.prevent="adjustConsoleWidth(-12)"
        />
        <div class="workspace-detail-header">
          <span>运行检查台</span>
          <Button variant="ghost" size="icon-sm" title="关闭运行详情" @click="closeDetail">
            <X :size="16" aria-hidden="true" /><span class="sr-only">关闭运行详情</span>
          </Button>
        </div>
        <WorkspaceConsole
          :run="runs.currentRun"
          :activity="activity"
          :trace-entries="traceEntries"
          :tool-calls="runs.toolCalls"
          :sql-audits="runs.sqlAudits"
          :artifacts="runs.artifacts"
          :datalink-consumptions="runs.datalinkConsumptions"
          :trace-dag="runs.traceDag"
          :trace-dag-loading="runs.traceDagLoading"
          :trace-dag-error="runs.traceDagError"
          :answer-evidence-refs="currentAnswerEvidenceRefs"
          :answer-content="currentAnswerMessage?.content_text ?? ''"
          :answer-anchor-prefix="currentAnswerMessage ? answerAnchorPrefix(currentAnswerMessage) : ''"
          :selected-artifact-id="selectedArtifactId"
          :selected-detail-id="selectedDetailId"
          @select-artifact="selectArtifact"
          @select-detail="selectInspectorDetail"
          @close-inspection="closeInspection"
          @navigate-answer="navigateToAnswer"
          @select-run="selectRun"
        />
      </aside>
    </div>

    <Sheet v-model:open="workspace.leftDrawerOpen">
      <SheetContent side="left" class="workspace-sheet">
        <Button variant="ghost" size="icon-sm" title="关闭" @click="workspace.leftDrawerOpen = false">
          <X :size="16" aria-hidden="true" /><span class="sr-only">关闭</span>
        </Button>
        <div class="workspace-side-content">
          <div class="side-heading">
            <div class="side-heading-title">
              <span>会话</span>
            </div>
            <div class="side-heading-actions">
              <Button variant="ghost" size="icon-sm" title="全部会话" @click="openSessionOverlay">
                <List :size="16" aria-hidden="true" /><span class="sr-only">全部会话</span>
              </Button>
              <Button variant="ghost" size="icon-sm" title="新建会话" @click="openNewSession">
                <Plus :size="16" aria-hidden="true" /><span class="sr-only">新建会话</span>
              </Button>
            </div>
          </div>
          <div class="session-list">
            <div
              v-for="session in sessions.sessions"
              :key="session.id"
              class="session-row"
              :class="{ active: session.id === sessions.currentSession?.id }"
            >
              <button type="button" class="session-row-main" @click="selectSession(session.id)">
                <span>{{ session.title }}</span>
                <small>{{ relativeActivityLabel(session.last_message_at) }}</small>
              </button>
              <Button
                variant="ghost"
                size="icon-sm"
                title="删除会话"
                @click="requestDeleteSession(session.id)"
              >
                <Trash2 :size="14" aria-hidden="true" /><span class="sr-only">删除会话</span>
              </Button>
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>

    <Sheet v-model:open="workspace.rightDrawerOpen" @update:open="handleRightDrawerOpen">
      <SheetContent side="right" class="workspace-sheet workspace-detail-sheet">
        <Button variant="ghost" size="icon-sm" title="关闭" @click="handleRightDrawerOpen(false)">
          <X :size="16" aria-hidden="true" /><span class="sr-only">关闭</span>
        </Button>
        <WorkspaceConsole
          :run="runs.currentRun"
          :activity="activity"
          :trace-entries="traceEntries"
          :tool-calls="runs.toolCalls"
          :sql-audits="runs.sqlAudits"
          :artifacts="runs.artifacts"
          :datalink-consumptions="runs.datalinkConsumptions"
          :trace-dag="runs.traceDag"
          :trace-dag-loading="runs.traceDagLoading"
          :trace-dag-error="runs.traceDagError"
          :answer-evidence-refs="currentAnswerEvidenceRefs"
          :answer-content="currentAnswerMessage?.content_text ?? ''"
          :answer-anchor-prefix="currentAnswerMessage ? answerAnchorPrefix(currentAnswerMessage) : ''"
          :selected-artifact-id="selectedArtifactId"
          :selected-detail-id="selectedDetailId"
          @select-artifact="selectArtifact"
          @select-detail="selectInspectorDetail"
          @close-inspection="closeInspection"
          @navigate-answer="navigateToAnswer"
          @select-run="selectRun"
        />
      </SheetContent>
    </Sheet>

    <CatalogOverlay
      :open="sessionOverlayOpen"
      title="全部会话"
      description="按标题查找并打开任意会话。删除不可恢复。"
      search-placeholder="搜索会话标题"
      :query="sessionOverlayDraft"
      :items="sessionOverlayCatalog"
      :total="sessionOverlayTotal"
      :page="sessionOverlayPage"
      :page-size="OVERLAY_PAGE_SIZE"
      :loading="sessionOverlayLoading"
      empty="没有匹配的会话。"
      :current-id="sessions.currentSession?.id"
      @update:open="sessionOverlayOpen = $event"
      @update:query="onSessionOverlayQuery"
      @page="onSessionOverlayPage"
      @select="selectOverlaySession"
      @delete="requestDeleteSession"
    />

    <Dialog :open="pendingDeleteSessionId !== null" @update:open="onDeleteDialogOpen">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>删除会话</DialogTitle>
          <DialogDescription>{{ SESSION_DELETE_COPY }}</DialogDescription>
        </DialogHeader>
        <p class="dialog-label">即将删除「{{ pendingDeleteTitle }}」。</p>
        <DialogFooter>
          <Button variant="outline" :disabled="deletingSession" @click="cancelDeleteSession">取消</Button>
          <Button variant="destructive" :disabled="deletingSession" @click="confirmDeleteSession">删除</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="newSessionOpen">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建分析会话</DialogTitle>
          <DialogDescription>会话会绑定当前选择的数据源，之后可在没有运行任务时切换。</DialogDescription>
        </DialogHeader>
        <label class="dialog-label" for="session-datasource">数据源</label>
        <select id="session-datasource" v-model="newDatasourceId" class="dialog-select">
          <option value="" disabled>选择已就绪数据源</option>
          <option v-for="datasource in readyDatasources" :key="datasource.id" :value="datasource.id">
            {{ datasource.name }} / {{ datasource.type.toUpperCase() }}
          </option>
        </select>
        <p v-if="readyDatasources.length === 0" class="question-error">没有已就绪数据源，请先在数据源页面完成准备。</p>
        <DialogFooter>
          <Button variant="outline" @click="newSessionOpen = false">取消</Button>
          <Button :disabled="!newDatasourceId" @click="createNewSession">创建会话</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </section>
</template>

<style scoped>
.workspace { height: calc(100dvh - 48px); min-height: 560px; background: radial-gradient(circle at 8% 5%, var(--workspace-page-glow-a), transparent 28%), radial-gradient(circle at 88% 92%, var(--workspace-page-glow-b), transparent 26%), var(--workspace-page); padding: 12px; }
.workspace-grid { display: grid; height: 100%; grid-template-columns: var(--workspace-left-width, 238px) minmax(420px, 1fr); overflow: hidden; border: 1px solid var(--workspace-border); border-radius: 14px; background: linear-gradient(135deg, var(--workspace-glass-sheen), transparent 40%), var(--workspace-surface); box-shadow: var(--workspace-shadow-overlay); }
.workspace-grid.has-detail { grid-template-columns: var(--workspace-left-width, 238px) minmax(0, 1fr) minmax(320px, var(--workspace-console-width, 380px)); }
.workspace-left, .workspace-right { position: relative; min-width: 0; min-height: 0; background: linear-gradient(180deg, var(--workspace-glass-sheen), transparent 35%), var(--workspace-surface-subtle); backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px); }
.workspace-left { border-right: 1px solid var(--workspace-border); }
.workspace-right { display: flex; flex-direction: column; border-left: 1px solid var(--workspace-border); padding: 12px; }
.workspace-detail-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; color: var(--workspace-text); font-size: 12px; font-weight: 700; }
.workspace-resize-handle { position: absolute; z-index: 3; top: 0; bottom: 0; width: 8px; border: 0; background: transparent; cursor: col-resize; touch-action: none; }
.workspace-resize-handle::after { position: absolute; top: 50%; width: 2px; height: 36px; border-radius: 1px; background: transparent; content: ""; transform: translateY(-50%); }
.workspace-resize-handle:hover::after, .workspace-resize-handle:focus-visible::after { background: var(--workspace-focus); }
.workspace-resize-handle:focus-visible { outline: 0; }
.workspace-left-resize-handle { right: -5px; }.workspace-left-resize-handle::after { right: 3px; }.workspace-console-resize-handle { left: -5px; }.workspace-console-resize-handle::after { left: 3px; }
.workspace-side-content { display: flex; height: 100%; min-height: 0; flex-direction: column; gap: 12px; padding: 12px; }.workspace-left.is-collapsed .workspace-side-content { display: none; }.workspace-left-expand { position: absolute; top: 10px; left: 50%; transform: translateX(-50%); }
.side-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--workspace-text); font-size: 12px; font-weight: 700; }.side-heading-title { display: flex; min-width: 0; align-items: center; gap: 6px; }.side-heading-actions { display: flex; align-items: center; gap: 2px; }.session-list, .message-timeline { min-height: 0; overflow: auto; }.session-list { display: grid; flex: 1; align-content: start; align-items: stretch; grid-auto-rows: max-content; gap: 2px; }
.session-row { display: grid; width: 100%; min-width: 0; grid-template-columns: minmax(0, 1fr) auto; align-items: center; border-radius: var(--workspace-radius-sm); }.session-row:hover { background: var(--workspace-surface-hover); }.session-row.active { background: var(--workspace-surface-selected); }.session-row-main { display: grid; min-width: 0; align-content: start; gap: 3px; padding: 8px; border: 0; background: transparent; color: inherit; cursor: pointer; text-align: left; }.session-row-main span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }.session-row-main small, .side-meta { color: var(--workspace-text-muted); font-size: 11px; }
.side-section { display: grid; gap: 6px; border-top: 1px solid var(--workspace-border); padding-top: 12px; }.side-section strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }.side-empty { margin: 0; color: var(--workspace-text-muted); font-size: 12px; line-height: 1.5; }.day-separator { display: flex; justify-content: center; color: var(--workspace-text-muted); font-size: 11px; }
.workspace-main { display: grid; min-width: 0; min-height: 0; grid-template-rows: auto minmax(0, 1fr) auto; background: var(--workspace-surface); }.conversation-header { display: flex; min-height: 64px; align-items: center; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--workspace-border); padding: 12px clamp(16px, 3vw, 36px); }.conversation-header h1 { margin: 0; color: var(--workspace-text); font-size: 15px; font-weight: 700; }.conversation-header p { margin: 4px 0 0; color: var(--workspace-text-muted); font-size: 12px; }.conversation-header-actions { display: flex; align-items: center; gap: 5px; }
 .message-timeline { display: grid; align-content: start; grid-auto-rows: max-content; gap: 15px; padding: 24px clamp(18px, 4vw, 56px); scroll-behavior: auto; }.message { width: min(100%, 760px); min-width: 0; max-width: 100%; }.message.user { justify-self: end; width: min(100%, 620px); border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius); background: var(--workspace-surface-inset); padding: 11px 13px; }.message.assistant { padding: 0 4px; }.message-role { display: flex; min-height: 18px; align-items: center; gap: 7px; margin-bottom: 7px; color: var(--workspace-text-muted); font-size: 11px; font-weight: 700; }.message-role-dot { width: 6px; height: 6px; flex: none; border-radius: 999px; background: var(--workspace-text-subtle); }.message.user p, .message-status-copy p { margin: 0; color: var(--workspace-text); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.72; }.answer-charts { display: grid; min-width: 0; gap: 10px; margin-top: 12px; }.answer-charts :deep(.artifact-viewer) { border-top: 0; padding-top: 0; }.answer-charts :deep(.image-artifact img) { max-height: min(420px, 50vh); }.answer-provenance { margin-top: 9px !important; color: var(--workspace-text-muted) !important; font-size: 11px !important; line-height: 1.45 !important; }.message-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; }.message-action { min-height: 32px; border-color: var(--workspace-border-strong); background: var(--workspace-surface-subtle); padding-inline: 10px; color: var(--workspace-text); box-shadow: 0 1px 1px color-mix(in srgb, var(--workspace-text) 5%, transparent); transition: border-color .16s ease, background-color .16s ease, color .16s ease, transform .16s ease; }.message-action:hover { border-color: color-mix(in srgb, var(--workspace-focus) 45%, var(--workspace-border)); background: var(--workspace-surface-selected); color: var(--workspace-focus); }.message-action:focus-visible { outline: 2px solid color-mix(in srgb, var(--workspace-focus) 65%, transparent); outline-offset: 2px; }.message-action:active { transform: translateY(1px); }.message-action-icon { color: var(--workspace-focus); }.message-artifact-link .message-action-icon { color: var(--workspace-tool-artifact); }.message-action-count { margin-left: 2px; border-left: 1px solid var(--workspace-border); padding-left: 8px; color: var(--workspace-text-muted); font-size: 10px; font-variant-numeric: tabular-nums; }.message-action:hover .message-action-count { border-left-color: color-mix(in srgb, var(--workspace-focus) 28%, var(--workspace-border)); color: currentColor; }.rich-markdown :deep(.answer-heading-anchor) { scroll-margin-top: 20px; }
.question-box { display: grid; gap: 8px; border-top: 1px solid var(--workspace-border); background: var(--workspace-surface); padding: 12px clamp(18px, 4vw, 56px) 16px; }.composer-context, .question-actions { display: flex; min-height: 26px; align-items: center; justify-content: space-between; gap: 10px; color: var(--workspace-text-muted); font-size: 12px; }.composer-context { justify-content: flex-start; }.composer-context span + span { border-left: 1px solid var(--workspace-border); padding-left: 10px; }.question-actions > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.question-hint { margin: 0; color: var(--workspace-state-warning); font-size: 12px; line-height: 1.45; }.question-error { color: var(--workspace-state-error); }
.workspace-empty { display: grid; place-content: center; justify-items: center; gap: 10px; height: 100%; padding: 24px; color: var(--workspace-text-muted); text-align: center; }.workspace-empty h1, .workspace-empty p { margin: 0; }.workspace-empty h1 { color: var(--workspace-text); font-size: 18px; }.workspace-empty p { max-width: 360px; font-size: 13px; line-height: 1.6; }.workspace-mobile-tools { display: none; }.workspace.workspace-narrow .workspace-grid { height: calc(100% - 34px); grid-template-columns: var(--workspace-left-width, 238px) minmax(0, 1fr); }.workspace.workspace-narrow .workspace-mobile-tools { display: flex; height: 34px; align-items: center; justify-content: space-between; padding: 0 0 8px; }.workspace-mobile-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 650; }.workspace-sheet { gap: 8px; }.workspace-sheet > :first-child { align-self: flex-end; }.workspace-detail-sheet { display: flex; min-height: 0; flex-direction: column; }.workspace-detail-sheet :deep(.workspace-console) { min-height: 0; }
.dialog-label { color: var(--workspace-text); font-size: 12px; font-weight: 700; }.dialog-select { width: 100%; min-height: 36px; border: 1px solid var(--workspace-border-strong); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface); padding: 0 9px; color: var(--workspace-text); font-size: 13px; }.status-complete { border-color: color-mix(in srgb, var(--workspace-state-success) 45%, var(--workspace-border)); color: var(--workspace-state-success); }.status-warning { border-color: color-mix(in srgb, var(--workspace-state-warning) 45%, var(--workspace-border)); color: var(--workspace-state-warning); }.status-running { border-color: color-mix(in srgb, var(--workspace-state-running) 45%, var(--workspace-border)); color: var(--workspace-state-running); }.status-error { border-color: color-mix(in srgb, var(--workspace-state-error) 38%, var(--workspace-border)); color: var(--workspace-state-error); }.status-muted { color: var(--workspace-text-muted); }
@media (max-width: 760px) { .workspace { height: calc(100dvh - 48px); min-height: 500px; padding: 8px; }.workspace.workspace-narrow .workspace-grid, .workspace.workspace-narrow .workspace-grid.has-detail { grid-template-columns: minmax(0, 1fr); }.workspace-left { display: none; }.workspace-resize-handle { display: none; }.conversation-header { padding: 10px 12px; }.message-timeline { padding: 16px; }.message.user { width: min(100%, 92%); }.answer-charts :deep(.image-artifact img) { max-width: 100%; max-height: min(240px, 40vh); }.question-box { padding: 10px 12px 12px; }.question-actions { align-items: flex-end; }.question-actions > span { white-space: normal; }.workspace-mobile-tools { padding-bottom: 6px; } }
.message-timeline { overflow-x: hidden; }
</style>
