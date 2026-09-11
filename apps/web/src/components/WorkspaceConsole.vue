<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { CheckCircle2, Database, FileText, LoaderCircle, Maximize2, X, XCircle } from "@lucide/vue";
import { DialogClose, DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from "reka-ui";

import { listSessionRuns } from "@/api/sessions";
import type { DataLinkConsumption, DataLinkConsumptionList, Run, RunArtifact, SqlAudit, ToolCall, TraceDag, TraceDagNode } from "@/api/types";
import CatalogOverlay, { type CatalogOverlayItem } from "@/components/CatalogOverlay.vue";
import DataLinkConsumptionDetail from "@/components/DataLinkConsumptionDetail.vue";
import RunTraceDag from "@/components/RunTraceDag.vue";
import TraceOverlay from "@/components/TraceOverlay.vue";
import RunInspectionContent from "@/components/RunInspectionContent.vue";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  createEvidenceCatalog,
  isEvidenceActionable,
  resolveAnswerEvidence,
  type ResolvedEvidence,
} from "@/lib/evidenceResolver";
import { extractAnswerHeadings } from "@/lib/answerNavigation";
import type { RunActivity } from "@/lib/runActivity";
import type { RunTraceEntry } from "@/lib/runTrace";
import { buildRunInspectionGroups, inspectionGroupForRef, inspectionGroupSummary } from "@/lib/runInspection";
import { presentTraceEntries } from "@/lib/tracePresentation";
import { parseApiInstant, runStartLabel } from "@/lib/workspaceTime";
import { useSessionStore } from "@/stores/sessionStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const props = defineProps<{
  run: Run | null;
  activity: RunActivity | null;
  traceEntries: readonly RunTraceEntry[];
  toolCalls: readonly ToolCall[];
  sqlAudits: readonly SqlAudit[];
  artifacts: readonly RunArtifact[];
  datalinkConsumptions?: DataLinkConsumptionList | null;
  traceDag?: TraceDag | null;
  traceDagLoading?: boolean;
  traceDagError?: string | null;
  answerEvidenceRefs?: readonly string[];
  answerContent?: string;
  answerAnchorPrefix?: string;
  selectedArtifactId: string;
  selectedDetailId: string;
}>();

const emit = defineEmits<{
  selectArtifact: [artifactId: string];
  selectDetail: [detailId: string];
  navigateAnswer: [anchorId: string];
  closeInspection: [];
  selectRun: [runId: string];
}>();

const OVERLAY_PAGE_SIZE = 20;
const workspace = useWorkspaceStore();
const sessions = useSessionStore();
const runOverlayOpen = ref(false);
const runOverlayDraft = ref("");
const runOverlayQuery = ref("");
const runOverlayPage = ref(1);
const runOverlayItems = ref<Run[]>([]);
const runOverlayTotal = ref(0);
const runOverlayLoading = ref(false);
let runSearchTimer = 0;
const runOverlayCatalog = computed<CatalogOverlayItem[]>(() =>
  runOverlayItems.value.map((item) => ({
    id: item.id,
    title: item.question,
    meta: runStartLabel(item),
  })),
);
const startTimeLabel = computed(() => (props.run === null ? "-" : runStartLabel(props.run) || "-"));
const pageArtifactId = ref("");
const detailOpen = ref(false);
const inspectionDetailId = ref(props.selectedDetailId);
const traceOverlayOpen = ref(false);
const consumptionOpen = ref(false);
const selectedConsumptionId = ref("");
const selectedConsumption = computed((): DataLinkConsumption | null =>
  props.datalinkConsumptions?.items.find((item) => item.id === selectedConsumptionId.value)
  ?? props.datalinkConsumptions?.items[0]
  ?? null,
);
const selectedDagNodeId = ref("");
const displayTraceEntries = computed(() => presentTraceEntries(props.traceEntries, props.run?.id ?? props.traceDag?.run_id ?? ""));
const currentToolCalls = computed(() =>
  props.run === null ? [] : props.toolCalls.filter((tool) => tool.run_id === props.run?.id),
);
const currentSqlAudits = computed(() =>
  props.run === null ? [] : props.sqlAudits.filter((audit) => audit.run_id === props.run?.id),
);
const currentArtifacts = computed(() =>
  props.run === null ? [] : props.artifacts.filter((artifact) => artifact.run_id === props.run?.id),
);
const answerEvidence = computed(() => {
  const refs = props.answerEvidenceRefs ?? [];
  if (props.run === null || refs.length === 0) return null;
  const catalog = createEvidenceCatalog({
    run: props.run,
    toolCalls: props.toolCalls,
    sqlAudits: props.sqlAudits,
    artifacts: props.artifacts,
  });
  return resolveAnswerEvidence(refs, catalog);
});
const answerHeadings = computed(() => {
  const content = props.answerContent ?? "";
  const anchorPrefix = props.answerAnchorPrefix ?? "";
  if (!content || !anchorPrefix) return [];
  return extractAnswerHeadings(content, anchorPrefix);
});
const inspectionGroups = computed(() => buildRunInspectionGroups(props.run?.id ?? "", currentToolCalls.value, currentSqlAudits.value, currentArtifacts.value));
const nodeSources = computed(() => Object.fromEntries((props.traceDag?.nodes ?? []).flatMap((node) => {
  const ref = node.artifact_id ? `artifact:${node.artifact_id}` : node.tool_call_id ? `tool:${node.tool_call_id}` : "";
  const group = inspectionGroupForRef(inspectionGroups.value, ref);
  return group ? [[node.id, `${group.title} · ${group.artifacts.length} 个结果`]] : [];
})));
const selectedGroup = computed(() => {
  const direct = inspectionGroupForRef(inspectionGroups.value, inspectionDetailId.value);
  if (direct) return direct;
  const toolId = selectedActivity.value?.toolCallId ?? selectedTraceEntry.value?.toolCallId;
  return toolId ? inspectionGroupForRef(inspectionGroups.value, `tool:${toolId}`) : undefined;
});
const evidenceGroups = computed(() => {
  const seen = new Set<string>();
  return (answerEvidence.value?.evidences ?? []).flatMap((evidence) => {
    const group = isEvidenceActionable(evidence) ? inspectionGroupForRef(inspectionGroups.value, evidence.detailId) : undefined;
    const key = group?.id ?? evidence.ref;
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ key, evidence, label: group?.title ?? evidence.label, summary: group ? inspectionGroupSummary(group) : evidence.label }];
  });
});
const inspectionTitle = computed(() => selectedGroup.value?.title ?? selectedActivity.value?.title ?? selectedTraceEntry.value?.title ?? (schemaSelected.value ? "本次运行的数据结构" : "运行详情"));
const selectedActivity = computed(
  () => props.activity?.items.find((item) => item.id === inspectionDetailId.value) ?? null,
);
const selectedTraceEntry = computed(
  () => displayTraceEntries.value.find((entry) => entry.detailId === inspectionDetailId.value)
    ?? props.traceEntries.find((entry) => entry.detailId === inspectionDetailId.value) ?? null,
);
const schemaSelected = computed(() => inspectionDetailId.value === "schema");
const progressEntries = computed(() => props.activity?.displayEntries.filter(
  (entry) => entry.kind === "step"
    || (entry.item.kind !== "artifact"
      && entry.item.kind !== "answer"
      && !entry.item.id.startsWith("final-answer:")),
) ?? []);
const completedStepCount = computed(
  () => progressEntries.value.filter((entry) => (
    entry.kind === "step" ? entry.step.status : entry.item.status
  ) === "succeeded").length,
);
const progressPercent = computed(() => {
  const total = progressEntries.value.length;
  if (total === 0) return 0;
  return Math.round((completedStepCount.value / total) * 100);
});
const activityFinished = computed(() => props.activity?.status === "succeeded"
  || props.activity?.status === "failed" || props.activity?.status === "canceled");
const progressLabel = computed(() => {
  const count = `${completedStepCount.value}/${progressEntries.value.length}`;
  return props.activity?.completionKind === "clarification" ? `${count} 个准备项完成` : `${count} 项完成`;
});

watch(
  () => props.selectedArtifactId,
  (artifactId) => {
    pageArtifactId.value = artifactId;
    if (artifactId) {
      inspectionDetailId.value = `artifact:${artifactId}`;
      detailOpen.value = true;
    }
  },
  { immediate: true },
);

watch(
  currentArtifacts,
  (artifacts) => {
    if (pageArtifactId.value && !artifacts.some((artifact) => artifact.id === pageArtifactId.value)) {
      pageArtifactId.value = "";
      detailOpen.value = false;
    }
  },
);

watch(() => props.selectedDetailId, (id) => {
  if (id) { inspectionDetailId.value = id; detailOpen.value = true; }
}, { immediate: true });
watch(() => props.run?.id, () => { detailOpen.value = false; inspectionDetailId.value = ""; pageArtifactId.value = ""; });

function openDetail(id: string): void {
  inspectionDetailId.value = id;
  detailOpen.value = true;
  emit("selectDetail", id);
}

function updateDetailOpen(open: boolean): void {
  detailOpen.value = open;
  if (!open) emit("closeInspection");
}

watch(
  () => props.traceDag?.run_id,
  () => {
    selectedDagNodeId.value = "";
    traceOverlayOpen.value = false;
  },
);

watch(
  [traceOverlayOpen, () => props.traceDag?.nodes],
  ([open]) => {
    if (!open) return;
    const nodes = props.traceDag?.nodes ?? [];
    const current = nodes.find((node) => node.id === selectedDagNodeId.value);
    if (current && current.kind !== "run-start") return;
    selectedDagNodeId.value = preferredTraceNode(nodes)?.id ?? "";
  },
  { deep: true },
);
const selectedStepNumber = computed(() => {
  const toolCallId = selectedActivity.value?.toolCallId;
  if (toolCallId === null || toolCallId === undefined) return null;
  const index = props.activity?.steps.findIndex((step) => step.toolCallIds.includes(toolCallId)) ?? -1;
  return index < 0 ? null : index + 1;
});
const durationLabel = computed(() => {
  const started = props.run?.started_at ? parseApiInstant(props.run.started_at) : null;
  if (started === null) return "-";
  const finished = props.run?.finished_at ? parseApiInstant(props.run.finished_at) : null;
  if (finished === null) return "进行中";
  return formatDuration(Math.max(0, finished.getTime() - started.getTime()));
});
const traceTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function formatDuration(elapsedMs: number): string {
  if (elapsedMs < 1_000) return `${elapsedMs} ms`;
  if (elapsedMs >= 60_000) {
    const seconds = Math.round(elapsedMs / 100) / 10;
    return `${Math.floor(seconds / 60)} 分 ${Number((seconds % 60).toFixed(1))} 秒`;
  }
  return `${(elapsedMs / 1_000).toFixed(1)} 秒`;
}

function formatTraceTime(timestamp: string): string {
  const parsed = parseApiInstant(timestamp);
  return parsed === null ? "时间未记录" : traceTimeFormatter.format(parsed);
}

function formatArtifactSize(sizeBytes: number): string {
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`;
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

function runStatusLabel(): string {
  if (props.activity?.status === "succeeded") {
    if (props.activity.completionKind === "partial") return "部分完成";
    if (props.activity.completionKind === "clarification") return "需要补充信息";
    return "已完成";
  }
  if (props.activity?.status === "failed") return "未完成";
  if (props.activity?.status === "canceled") return "已取消";
  if (props.activity?.status === "queued") return "等待中";
  if (props.activity?.status === "running") return "运行中";
  return "未开始";
}

function consoleStatusClass(): string {
  return props.activity?.completionKind === "partial" || props.activity?.completionKind === "clarification"
    ? "status-partial"
    : `status-${props.activity?.status ?? "unknown"}`;
}

function progressStatusClass(): string {
  return props.activity?.completionKind === "partial" || props.activity?.completionKind === "clarification"
    ? "progress-partial"
    : `progress-${props.activity?.status ?? "unknown"}`;
}

function itemStatusLabel(status: string | null | undefined): string {
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "未完成";
  if (status === "canceled") return "已取消";
  if (status === "running") return "进行中";
  if (status === "queued") return "等待中";
  return "未记录";
}

function selectTraceEntry(entry: RunTraceEntry): void {
  selectedDagNodeId.value = props.traceDag?.nodes.find((node) =>
    node.detail_event_seq === entry.seq
      || node.action_records.some((action) => action.event_seq === entry.seq),
  )?.id ?? "";
  if (entry.artifactId !== null) {
    selectArtifact(entry.artifactId);
    return;
  }
  openDetail(entry.detailId);
}

function selectDagNode(node: TraceDagNode): void {
  selectedDagNodeId.value = node.id;
  if (node.artifact_id !== null && currentArtifacts.value.some((artifact) => artifact.id === node.artifact_id)) {
    selectArtifact(node.artifact_id);
    traceOverlayOpen.value = false;
    return;
  }

  if (node.tool_call_id !== null && currentToolCalls.value.some((tool) => tool.id === node.tool_call_id)) {
    openDetail(`tool:${node.tool_call_id}`);
    traceOverlayOpen.value = false;
    return;
  }

  const detailSeq = node.detail_event_seq
    ?? [...node.action_records].sort((left, right) => left.event_seq - right.event_seq)[0]?.event_seq;
  const traceEntry = detailSeq === undefined || detailSeq === null
    ? undefined
    : props.traceEntries.find((entry) => entry.seq === detailSeq);
  if (traceEntry !== undefined) {
    openDetail(traceEntry.detailId);
  }
  traceOverlayOpen.value = false;
}

function selectOverlayNode(node: TraceDagNode): void {
  selectedDagNodeId.value = node.id;
}

function selectOverlayEntry(entry: RunTraceEntry): void {
  selectedDagNodeId.value = props.traceDag?.nodes.find((node) =>
    node.detail_event_seq === entry.seq
      || node.action_records.some((action) => action.event_seq === entry.seq),
  )?.id ?? selectedDagNodeId.value;
}

function openOverlayNode(node: TraceDagNode): void {
  traceOverlayOpen.value = false;
  selectDagNode(node);
}

function openTraceOverlay(): void {
  const nodes = props.traceDag?.nodes ?? [];
  const current = nodes.find((node) => node.id === selectedDagNodeId.value);
  if (!current || current.kind === "run-start") {
    selectedDagNodeId.value = preferredTraceNode(nodes)?.id ?? "";
  }
  traceOverlayOpen.value = true;
}

function preferredTraceNode(nodes: readonly TraceDagNode[]): TraceDagNode | undefined {
  return nodes.find((node) => node.kind === "agent-turn")
    ?? nodes.find((node) => node.kind === "tool")
    ?? nodes.find((node) => node.kind === "final-answer")
    ?? nodes.find((node) => node.kind === "preparation")
    ?? nodes[0];
}

function selectAudit(audit: SqlAudit): void {
  selectedDagNodeId.value = props.traceDag?.nodes.find((node) => node.tool_call_id === audit.tool_call_id)?.id ?? "";
  openDetail(`audit:${audit.id}`);
}

function selectArtifact(artifactId: string): void {
  selectedDagNodeId.value = props.traceDag?.nodes.find((node) => node.artifact_id === artifactId)?.id ?? "";
  pageArtifactId.value = artifactId;
  emit("selectArtifact", artifactId);
  openDetail(`artifact:${artifactId}`);
}

function selectAnswerEvidence(evidence: ResolvedEvidence): void {
  if (!isEvidenceActionable(evidence)) return;
  if (evidence.kind === "artifact") {
    selectArtifact(evidence.artifact.id);
    return;
  }
  openDetail(evidence.detailId);
}

function selectAnswerHeading(anchorId: string): void {
  emit("navigateAnswer", anchorId);
}

async function refreshRunOverlay(): Promise<void> {
  const sessionId = sessions.currentSession?.id;
  if (sessionId === undefined) {
    runOverlayItems.value = [];
    runOverlayTotal.value = 0;
    return;
  }
  runOverlayLoading.value = true;
  try {
    const page = await listSessionRuns(
      sessionId,
      runOverlayPage.value,
      OVERLAY_PAGE_SIZE,
      runOverlayQuery.value,
    );
    runOverlayItems.value = page.items;
    runOverlayTotal.value = page.total;
    const pageCount = Math.max(1, Math.ceil(page.total / OVERLAY_PAGE_SIZE) || 1);
    if (runOverlayPage.value > pageCount) {
      runOverlayPage.value = pageCount;
      if (page.total > 0) await refreshRunOverlay();
    }
  } finally {
    runOverlayLoading.value = false;
  }
}

function openRunOverlay(): void {
  runOverlayDraft.value = "";
  runOverlayQuery.value = "";
  runOverlayPage.value = 1;
  runOverlayOpen.value = true;
  void refreshRunOverlay();
}

function onRunOverlayQuery(query: string): void {
  runOverlayDraft.value = query;
  window.clearTimeout(runSearchTimer);
  runSearchTimer = window.setTimeout(() => {
    runOverlayQuery.value = query;
    runOverlayPage.value = 1;
    void refreshRunOverlay();
  }, 250);
}

function onRunOverlayPage(page: number): void {
  runOverlayPage.value = page;
  void refreshRunOverlay();
}

function selectCatalogRun(runId: string): void {
  runOverlayOpen.value = false;
  emit("selectRun", runId);
}

watch(
  () => sessions.currentSession?.id,
  () => {
    runOverlayOpen.value = false;
    runOverlayItems.value = [];
    runOverlayTotal.value = 0;
    runOverlayPage.value = 1;
    runOverlayDraft.value = "";
    runOverlayQuery.value = "";
  },
);

onBeforeUnmount(() => {
  window.clearTimeout(runSearchTimer);
});

</script>

<template>
  <section class="workspace-console" aria-label="当前运行检查台">
    <header class="console-summary">
      <div class="console-summary-copy">
        <span>本次运行</span>
      </div>
      <span class="console-status" :class="consoleStatusClass()">
        <LoaderCircle v-if="activity?.status === 'queued' || activity?.status === 'running'" :size="14" aria-hidden="true" />
        <CheckCircle2 v-else-if="activity?.status === 'succeeded'" :size="14" aria-hidden="true" />
        <XCircle v-else-if="activity?.status === 'failed' || activity?.status === 'canceled'" :size="14" aria-hidden="true" />
        {{ runStatusLabel() }}
      </span>
    </header>
    <p v-if="activity?.status === 'running' || activity?.status === 'queued'" class="console-live-status">{{ activity.summary }}</p>

    <dl class="console-metrics">
      <div><dt>开始时间</dt><dd>{{ startTimeLabel }}</dd></div>
      <div><dt>总耗时</dt><dd>{{ durationLabel }}</dd></div>
      <div><dt>工具调用</dt><dd>{{ activity?.toolCount ?? 0 }}</dd></div>
      <div><dt>产物</dt><dd>{{ activity?.artifactCount ?? 0 }}</dd></div>
    </dl>

    <Tabs v-model="workspace.consoleTab" class="console-tabs-root">
      <TabsList class="console-tabs" aria-label="运行检查台内容">
        <TabsTrigger value="runs">Run</TabsTrigger>
        <TabsTrigger value="overview">概览</TabsTrigger>
        <TabsTrigger value="trace">执行过程</TabsTrigger>
        <TabsTrigger value="outputs">产物<span v-if="currentArtifacts.length" class="tab-count">{{ currentArtifacts.length }}</span></TabsTrigger>
      </TabsList>

      <TabsContent value="runs" class="console-panel">
        <div class="run-catalog-heading">
          <span>本会话最近 Run</span>
          <button
            type="button"
            class="run-catalog-all"
            title="全部 Run"
            aria-label="查看全部 Run"
            @click="openRunOverlay"
          >查看全部</button>
        </div>
        <p v-if="sessions.runs.length === 0" class="console-empty">当前会话还没有 Run。</p>
        <div v-else class="run-catalog">
          <button
            v-for="item in sessions.runs"
            :key="item.id"
            type="button"
            class="run-catalog-row"
            :class="{ active: item.id === run?.id }"
            @click="emit('selectRun', item.id)"
          >
            <span>{{ item.question }}</span>
            <small>{{ runStartLabel(item) }}</small>
          </button>
        </div>
      </TabsContent>

      <TabsContent value="overview" class="console-panel">
        <section v-if="run" class="overview-question">
          <span>当前问题</span>
          <p>{{ run.question }}</p>
        </section>
        <section v-if="answerHeadings.length" class="answer-navigation" aria-label="答案导航">
          <header><strong>答案导航</strong><span>{{ answerHeadings.length }} 个标题</span></header>
          <nav class="answer-navigation-list" aria-label="答案标题">
            <button
              v-for="heading in answerHeadings"
              :key="heading.id"
              type="button"
              class="answer-navigation-item"
              :style="{ paddingLeft: `${8 + (heading.level - 1) * 10}px` }"
              @click="selectAnswerHeading(heading.id)"
            >
              {{ heading.title }}
            </button>
          </nav>
        </section>
        <section v-if="evidenceGroups.length" class="overview-evidence" aria-label="本答案依据">
          <header><strong>本答案依据</strong><span>{{ evidenceGroups.length }} 组</span></header>
          <div class="overview-evidence-list">
            <button
              v-for="item in evidenceGroups"
              :key="item.key"
              type="button"
              class="overview-evidence-item"
              :disabled="!isEvidenceActionable(item.evidence)"
              :title="'reason' in item.evidence ? item.evidence.reason : item.summary"
              @click="selectAnswerEvidence(item.evidence)"
            >
              <span class="evidence-group-copy"><strong>{{ item.label }}</strong><small>{{ item.summary }}</small></span>
              <Maximize2 v-if="isEvidenceActionable(item.evidence)" :size="14" aria-label="打开独立详情" />
            </button>
          </div>
        </section>
        <section v-if="progressEntries.length && !activityFinished" class="overview-progress" aria-label="分析进度">
          <header><strong>{{ activity?.completionKind === "clarification" ? "准备进度" : "分析进度" }}</strong><span>{{ progressLabel }}</span></header>
          <div class="progress-track" role="progressbar" :aria-valuenow="progressPercent" aria-valuemin="0" aria-valuemax="100">
            <span :class="progressStatusClass()" :style="{ width: `${progressPercent}%` }" />
          </div>
        </section>
        <p v-if="!activity" class="console-empty">选择一个 Run 后查看本次分析。</p>
      </TabsContent>

      <TabsContent value="trace" class="console-panel">
        <RunTraceDag
          :dag="traceDag ?? null"
          :loading="traceDagLoading"
          :error="traceDagError"
          :selected-node-id="selectedDagNodeId"
          :show-action-records="false"
          :node-sources="nodeSources"
          allow-fullscreen
          @select-node="selectDagNode"
          @fullscreen="openTraceOverlay"
        />

        <details class="linear-trace">
          <summary class="trace-section-heading">
            <strong>事件明细</strong>
            <span>{{ displayTraceEntries.length }} 条</span>
          </summary>
        <ol v-if="traceEntries.length" class="trace-list" aria-label="原始运行轨迹">
          <li v-for="entry in displayTraceEntries" :key="entry.id" class="trace-row" :class="[`trace-${entry.status}`, { active: entry.detailId === selectedDetailId || entry.artifactId === selectedArtifactId }]">
            <span class="trace-index">#{{ entry.seq }}</span>
            <button type="button" class="trace-action" :title="entry.eventType" @click="selectTraceEntry(entry)">
              <strong>{{ entry.title }}</strong>
              <small><span>{{ entry.summary }}</span></small>
              <small v-if="entry.resultSummary" class="trace-result"><span>结果：{{ entry.resultSummary }}</span></small>
            </button>
            <span class="trace-facts"><time :datetime="entry.timestamp">{{ formatTraceTime(entry.timestamp) }}</time><em>{{ entry.statusLabel }}</em></span>
          </li>
        </ol>
        <p v-else class="console-empty">分析开始后显示事件记录。</p>
        </details>

        <section class="audit-trace" aria-label="DataLink 消费记录">
          <h3>DataLink 消费记录</h3>
          <p v-if="datalinkConsumptions?.historical_status === 'missing'" class="console-empty">历史未记录完整内容，回放不会重新检索。</p>
          <button
            v-else-if="datalinkConsumptions?.items.length"
            type="button"
            class="audit-row"
            @click="consumptionOpen = true"
          >
            <span>查看准备阶段与工具阶段的安全语义传递</span>
            <small>{{ datalinkConsumptions.items.length }} 条 · 不表示模型采用</small>
          </button>
          <p v-else class="console-empty">本次 Run 没有 DataLink 消费记录。</p>
        </section>
        <section v-if="currentSqlAudits.length" class="audit-trace" aria-label="关联 SQL 审计">
          <h3>SQL 与查询结果</h3>
          <button v-for="audit in currentSqlAudits" :key="audit.id" type="button" class="audit-row" :class="{ active: selectedDetailId === `audit:${audit.id}` }" @click="selectAudit(audit)">
            <span><Database :size="14" aria-hidden="true" />{{ inspectionGroupForRef(inspectionGroups, `audit:${audit.id}`)?.title ?? "SQL 查询" }}</span>
            <small>{{ audit.referenced_tables.join("、") || "未引用表" }} · {{ inspectionGroupForRef(inspectionGroups, `audit:${audit.id}`)?.artifacts.length ?? 0 }} 个结果</small>
            <em :class="`status-${audit.status}`">{{ itemStatusLabel(audit.status) }}</em>
          </button>
        </section>
      </TabsContent>

      <TabsContent value="outputs" class="console-panel">
        <template v-if="currentArtifacts.length">
          <div class="artifact-list">
            <article
              v-for="artifact in currentArtifacts"
              :key="artifact.id"
              class="artifact-card"
              :class="{ active: artifact.id === selectedArtifactId }"
            >
              <button type="button" class="artifact-row" @click="selectArtifact(artifact.id)">
                <FileText :size="15" aria-hidden="true" />
                <span class="artifact-row-copy">
                  <strong>{{ artifact.title }}</strong>
                  <small>{{ inspectionGroupForRef(inspectionGroups, `artifact:${artifact.id}`)?.tool ? inspectionGroupForRef(inspectionGroups, `artifact:${artifact.id}`)?.title : "来源未记录" }} · {{ formatArtifactSize(artifact.size_bytes) }} · {{ formatTraceTime(artifact.created_at) }}</small>
                </span>
                <small class="artifact-type">{{ artifact.type }}</small>
              </button>
            </article>
          </div>
        </template>
        <p v-else class="console-empty">本次分析没有生成可查看产物。</p>
      </TabsContent>

    </Tabs>

    <DialogRoot :open="detailOpen" @update:open="updateDetailOpen">
      <DialogPortal>
        <DialogOverlay class="inspection-backdrop" />
        <DialogContent class="inspection-dialog" aria-label="运行独立详情">
          <header class="inspection-dialog-header">
            <DialogTitle>{{ inspectionTitle }}</DialogTitle>
            <DialogDescription class="sr-only">本次调用的输入、执行记录与关联结果。</DialogDescription>
            <DialogClose as-child><Button variant="ghost" size="icon-sm" title="关闭独立详情"><X :size="18" /><span class="sr-only">关闭独立详情</span></Button></DialogClose>
          </header>
          <div class="inspection-dialog-body">
        <RunInspectionContent v-if="selectedGroup" :group="selectedGroup" :selected-artifact-id="pageArtifactId" />
        <section v-else-if="selectedActivity" class="detail-section">
          <div class="detail-heading"><span>运行步骤</span><strong>{{ selectedActivity.title }}</strong></div>
          <dl class="detail-grid">
            <div><dt>状态</dt><dd>{{ itemStatusLabel(selectedActivity.status) }}</dd></div>
            <div v-if="selectedStepNumber !== null"><dt>调用顺序</dt><dd>第 {{ selectedStepNumber }} 步</dd></div>
            <div v-if="selectedActivity.detail"><dt>执行摘要</dt><dd>{{ selectedActivity.detail }}</dd></div>
          </dl>
        </section>

        <section v-else-if="selectedTraceEntry" class="detail-section">
          <div class="detail-heading"><span>运行事件 #{{ selectedTraceEntry.seq }}</span><strong>{{ selectedTraceEntry.title }}</strong></div>
          <dl class="detail-grid">
            <div><dt>事件类型</dt><dd><code>{{ selectedTraceEntry.eventType }}</code></dd></div>
            <div><dt>记录时间</dt><dd>{{ formatTraceTime(selectedTraceEntry.timestamp) }}</dd></div>
            <div><dt>事件状态</dt><dd>{{ selectedTraceEntry.statusLabel }}</dd></div>
            <div><dt>事件摘要</dt><dd>{{ selectedTraceEntry.summary }}</dd></div>
            <div v-if="selectedTraceEntry.resultSummary"><dt>结果摘要</dt><dd>{{ selectedTraceEntry.resultSummary }}</dd></div>
          </dl>
        </section>

        <section v-else-if="schemaSelected && run" class="detail-section">
          <div class="detail-heading"><span>数据结构</span><strong>本次 Run 的 Schema</strong></div>
          <dl class="detail-grid">
            <div><dt>Schema 修订</dt><dd>{{ run.schema_revision === null ? "未记录" : `r${run.schema_revision}` }}</dd></div>
            <div><dt>数据源状态</dt><dd>{{ run.datasource_deleted ? "数据源已删除" : "数据源可用" }}</dd></div>
            <div v-if="run.datalink_graph_version"><dt>图谱版本</dt><dd>{{ run.datalink_graph_version }}</dd></div>
          </dl>
          <p class="detail-note">这里显示本次 Run 固定的 Schema 修订；当前版本尚未保存历史字段快照。</p>
        </section>

        <p v-else class="console-empty">从调用轨迹或产物中选择一项，查看本次 Run 的安全详情。</p>
          </div>
        </DialogContent>
      </DialogPortal>
    </DialogRoot>

    <DataLinkConsumptionDetail
      v-if="consumptionOpen"
      :open="consumptionOpen"
      :list="datalinkConsumptions ?? null"
      :selected="selectedConsumption"
      @close="consumptionOpen = false"
      @select="selectedConsumptionId = $event"
    />

    <CatalogOverlay
      :open="runOverlayOpen"
      title="全部 Run"
      description="只列出当前会话的 Run，按问题查找。"
      search-placeholder="搜索问题"
      :query="runOverlayDraft"
      :items="runOverlayCatalog"
      :total="runOverlayTotal"
      :page="runOverlayPage"
      :page-size="OVERLAY_PAGE_SIZE"
      :loading="runOverlayLoading"
      empty="没有匹配的 Run。"
      :current-id="run?.id"
      @update:open="runOverlayOpen = $event"
      @update:query="onRunOverlayQuery"
      @page="onRunOverlayPage"
      @select="selectCatalogRun"
    />

    <TraceOverlay
      v-model:open="traceOverlayOpen"
      :dag="traceDag ?? null"
      :loading="traceDagLoading"
      :error="traceDagError"
      :selected-node-id="selectedDagNodeId"
      :run="run"
      :trace-entries="traceEntries"
      :artifacts="currentArtifacts"
      :tool-calls="currentToolCalls"
      :sql-audits="currentSqlAudits"
      @select-node="selectOverlayNode"
      @open-node="openOverlayNode"
      @select-entry="selectOverlayEntry"
    />
  </section>
</template>

<style scoped>
.workspace-console { display: flex; min-width: 0; min-height: 0; flex: 1; flex-direction: column; color: var(--workspace-text); }.console-summary { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; border-bottom: 1px solid var(--workspace-border); padding-bottom: 11px; }.console-summary-copy { display: grid; min-width: 0; gap: 3px; }.console-summary-copy span, .console-status { color: var(--workspace-text-muted); font-size: 11px; }.console-summary-copy strong { overflow: hidden; color: var(--workspace-text); font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }.console-status { display: inline-flex; flex: none; align-items: center; gap: 4px; white-space: nowrap; }.console-status.status-running, .console-status.status-queued { color: var(--workspace-state-running); }.console-status.status-succeeded { color: var(--workspace-state-success); }.console-status.status-partial { color: var(--workspace-state-warning); }.console-status.status-failed { color: var(--workspace-state-error); }.console-status.status-canceled { color: var(--workspace-state-canceled); }
.console-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; margin: 10px 0 12px; }.console-metrics div { min-width: 0; border-left: 2px solid var(--workspace-border-strong); padding-left: 7px; }.console-metrics dt { color: var(--workspace-text-muted); font-size: 10px; }.console-metrics dd { display: flex; align-items: center; gap: 3px; margin: 3px 0 0; color: var(--workspace-text); font-size: 12px; font-weight: 700; white-space: nowrap; }.workspace-console :deep(.console-tabs-root) { display: flex; min-height: 0; flex: 1; flex-direction: column; overflow: hidden; }.console-tabs { display: grid; width: 100%; grid-template-columns: repeat(4, minmax(0, 1fr)); }.console-tabs :deep(button) { min-width: 0; padding-inline: 5px; font-size: 11px; }.tab-count { margin-left: 3px; color: inherit; font-size: 10px; opacity: .75; }.console-panel { min-width: 0; min-height: 0; flex: 1 1 0; overflow: auto; overflow-x: hidden; padding-top: 12px; }.console-empty { margin: 0; color: var(--workspace-text-muted); font-size: 12px; line-height: 1.5; }
.overview-question { display: grid; gap: 4px; margin-bottom: 10px; border-left: 2px solid var(--workspace-focus); padding-left: 9px; }.overview-question span { color: var(--workspace-text-muted); font-size: 10px; }.overview-question p { margin: 0; color: var(--workspace-text); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }.answer-navigation { display: grid; gap: 6px; margin-bottom: 10px; border-bottom: 1px solid var(--workspace-border); padding-bottom: 10px; }.answer-navigation header { display: flex; justify-content: space-between; gap: 8px; color: var(--workspace-text-muted); font-size: 10px; }.answer-navigation header strong { color: var(--workspace-text); }.answer-navigation-list { display: grid; gap: 1px; }.answer-navigation-item { min-width: 0; border: 0; border-radius: var(--workspace-radius-sm); background: transparent; padding-top: 5px; padding-right: 7px; padding-bottom: 5px; color: var(--workspace-text); cursor: pointer; overflow: hidden; text-align: left; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; line-height: 1.35; }.answer-navigation-item:hover, .answer-navigation-item:focus-visible { background: var(--workspace-surface-selected); color: var(--workspace-focus); outline: none; }.overview-evidence { display: grid; gap: 6px; margin-bottom: 10px; border-top: 1px solid var(--workspace-border); border-bottom: 1px solid var(--workspace-border); padding: 9px 0; }.overview-evidence header { display: flex; justify-content: space-between; gap: 8px; color: var(--workspace-text-muted); font-size: 10px; }.overview-evidence header strong { color: var(--workspace-text); }.overview-evidence-list { display: grid; gap: 4px; }.overview-evidence-item { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 8px; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface); padding: 6px 8px; color: var(--workspace-text); cursor: pointer; text-align: left; }.overview-evidence-item:hover { border-color: var(--workspace-focus); background: var(--workspace-surface-selected); }.overview-evidence-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }.overview-evidence-item small { flex: none; color: var(--workspace-text-muted); font-size: 9px; }.overview-evidence-item:disabled { cursor: not-allowed; opacity: .65; }.overview-progress { display: grid; gap: 6px; margin-bottom: 10px; }.overview-progress header { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--workspace-text-muted); font-size: 11px; }.overview-progress header strong { color: var(--workspace-text); }.progress-track { height: 5px; overflow: hidden; border-radius: 999px; background: var(--workspace-surface-inset); }.progress-track span { display: block; height: 100%; border-radius: inherit; background: var(--workspace-state-running); transition: width .2s ease; }.progress-track .progress-succeeded { background: var(--workspace-state-success); }.progress-track .progress-partial { background: var(--workspace-state-warning); }.progress-track .progress-failed { background: var(--workspace-state-error); }.progress-track .progress-canceled { background: var(--workspace-state-canceled); }
.console-pages { display: flex; min-height: 34px; align-items: center; gap: 6px; border-bottom: 1px solid var(--workspace-border); padding-bottom: 7px; }.console-page-back { flex: none; }.console-page-active { display: inline-flex; min-width: 0; flex: 1; align-items: center; gap: 5px; overflow: hidden; color: var(--workspace-text); font-size: 12px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }.console-page-hint { margin: 9px 0 0; color: var(--workspace-text-muted); font-size: 11px; }.artifact-peer-page { min-height: 0; flex: 1 1 0; overflow: auto; padding-top: 10px; }
.linear-trace { display: grid; min-width: 0; gap: 4px; padding-top: 12px; }.trace-section-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }.trace-section-heading strong { color: var(--workspace-text); font-size: 11px; }.trace-section-heading span { color: var(--workspace-text-muted); font-size: 9px; }.trace-list { display: grid; margin: 0; padding: 0; list-style: none; }.trace-row { display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; align-items: center; gap: 7px; border-bottom: 1px solid var(--workspace-border); padding: 8px 0; }.trace-row.active { margin-inline: -6px; border-radius: var(--workspace-radius-sm); background: var(--workspace-surface-selected); padding-inline: 6px; }.trace-index { display: grid; min-width: 31px; height: 21px; place-items: center; border: 1px solid var(--workspace-border-strong); border-radius: var(--workspace-radius-sm); color: var(--workspace-text-muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 9px; font-weight: 700; }.trace-action { display: grid; min-width: 0; gap: 3px; border: 0; background: transparent; padding: 0; color: inherit; cursor: pointer; text-align: left; }.trace-action:hover strong { text-decoration: underline; }.trace-action strong { overflow: hidden; color: var(--workspace-text); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }.trace-action small { display: flex; min-width: 0; align-items: center; gap: 5px; overflow: hidden; color: var(--workspace-text-muted); font-size: 10px; white-space: nowrap; }.trace-action small code { flex: none; color: var(--workspace-text-subtle); font-size: 9px; }.trace-action small span { overflow: hidden; text-overflow: ellipsis; }.trace-action .trace-result { color: var(--workspace-text); }.trace-facts { display: grid; justify-items: end; gap: 2px; color: var(--workspace-text-muted); font-size: 9px; white-space: nowrap; }.trace-facts em { font-size: 10px; font-style: normal; }.trace-running .trace-facts em, .trace-queued .trace-facts em { color: var(--workspace-state-running); }.trace-succeeded .trace-facts em { color: var(--workspace-state-success); }.trace-failed .trace-facts em { color: var(--workspace-state-error); }.trace-canceled .trace-facts em { color: var(--workspace-state-canceled); }
.audit-trace { display: grid; gap: 4px; margin-top: 15px; }.audit-trace h3, .detail-block h3 { margin: 0 0 3px; color: var(--workspace-text); font-size: 11px; }.audit-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; align-items: center; gap: 6px; width: 100%; border: 0; border-bottom: 1px solid var(--workspace-border); background: transparent; padding: 7px 1px; color: inherit; cursor: pointer; text-align: left; }.audit-row:hover, .audit-row.active { background: var(--workspace-table-row-hover); }.audit-row span { display: inline-flex; min-width: 0; align-items: center; gap: 4px; overflow: hidden; color: var(--workspace-text); font-size: 11px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }.audit-row small { overflow: hidden; color: var(--workspace-text-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }.audit-row em { color: var(--workspace-text-muted); font-size: 10px; font-style: normal; white-space: nowrap; }.audit-row em.status-running, .audit-row em.status-queued { color: var(--workspace-state-running); }.audit-row em.status-succeeded { color: var(--workspace-state-success); }.audit-row em.status-failed { color: var(--workspace-state-error); }.audit-row em.status-canceled { color: var(--workspace-state-canceled); }
.artifact-list { display: grid; align-content: start; gap: 9px; }.artifact-card { min-width: 0; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius); background: var(--workspace-surface); padding: 5px; }.artifact-card.active { border-color: var(--workspace-focus); background: var(--workspace-surface-selected); }.artifact-row { display: grid; width: 100%; min-width: 0; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 7px; border: 0; border-radius: var(--workspace-radius-sm); background: transparent; padding: 7px 6px; color: inherit; cursor: pointer; text-align: left; }.artifact-row:hover { background: var(--workspace-surface-hover); }.artifact-row span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 700; }.artifact-row small { color: var(--workspace-text-muted); font-size: 10px; }.artifact-row .artifact-type { color: var(--workspace-tool-artifact); }.artifact-card-preview { margin: 2px 5px 5px; }
.detail-section { display: grid; gap: 12px; }.detail-heading { display: grid; gap: 3px; border-left: 2px solid var(--workspace-focus); padding-left: 9px; }.detail-heading span { color: var(--workspace-text-muted); font-size: 11px; }.detail-heading strong { color: var(--workspace-text); font-size: 13px; }.detail-grid { display: grid; gap: 7px; margin: 0; }.detail-grid div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 8px; }.detail-grid dt { color: var(--workspace-text-muted); font-size: 11px; }.detail-grid dd { min-width: 0; margin: 0; color: var(--workspace-text); font-size: 11px; overflow-wrap: anywhere; }.detail-grid code { font-size: 10px; }.detail-block { display: grid; gap: 7px; }.detail-summary { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); gap: 5px 8px; margin: 0; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface-subtle); padding: 8px; }.detail-summary dt, .detail-summary dd { min-width: 0; margin: 0; overflow-wrap: anywhere; font-size: 11px; }.detail-summary dt { color: var(--workspace-text-muted); }.detail-summary dd { color: var(--workspace-text); }.detail-code { max-height: 260px; margin: 0; overflow: auto; border: 1px solid var(--workspace-code-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-code-background); padding: 9px; color: var(--workspace-code-text); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 10px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }.detail-note, .detail-warning, .detail-error { margin: 0; border-left: 2px solid var(--workspace-border-strong); padding-left: 8px; color: var(--workspace-text-muted); font-size: 11px; line-height: 1.5; }.detail-warning { border-left-color: var(--workspace-state-warning); color: var(--workspace-state-warning); }.detail-error { border-left-color: var(--workspace-state-error); color: var(--workspace-state-error); }
@media (max-width: 520px) { .trace-row { grid-template-columns: 32px minmax(0, 1fr); }.trace-facts { grid-column: 2; grid-row: 2; grid-auto-flow: column; justify-content: start; justify-items: start; } }@media (prefers-reduced-motion: no-preference) { .console-status.status-running :deep(svg), .console-status.status-queued :deep(svg) { animation: console-spin 1.25s linear infinite; } }@keyframes console-spin { to { transform: rotate(360deg); } }
.artifact-row .artifact-row-copy { display: grid; min-width: 0; gap: 2px; overflow: hidden; font-size: inherit; font-weight: 400; white-space: normal; }.artifact-row-copy strong, .artifact-row-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.artifact-row-copy strong { font-size: 12px; }
.workspace-console { background: transparent; box-shadow: none; backdrop-filter: none; -webkit-backdrop-filter: none; }
.console-summary { align-items: center; min-height: 30px; border-bottom: 0; padding: 0; }
.console-summary-copy span { font-size: 12px; }
.console-status { font-size: 12px; font-weight: 600; }
.console-live-status { margin: 6px 0 0; color: var(--workspace-text); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.console-metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0; margin: 8px 0 14px; padding: 10px 0; border-block: 1px solid var(--workspace-border); }
.console-metrics div { border-left: 0; padding: 0 10px; }
.console-metrics div:first-child { padding-left: 0; }
.console-metrics div + div { border-left: 1px solid var(--workspace-border); }
.console-metrics dt { font-size: 11px; }
.console-metrics dd { margin-top: 5px; overflow: hidden; font-size: 13px; font-variant-numeric: tabular-nums; text-overflow: ellipsis; white-space: nowrap; }
.console-tabs { height: 36px; flex: none; grid-template-columns: repeat(4, minmax(0, 1fr)); }
.run-catalog-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; color: var(--workspace-text-muted); font-size: 11px; }
.run-catalog-all { height: auto; margin: 0; border: 0; background: transparent; padding: 0; color: var(--workspace-text-muted); font-size: 11px; font-weight: 500; cursor: pointer; }
.run-catalog-all:hover { color: var(--workspace-text); }
.run-catalog-all:focus-visible { outline: 2px solid color-mix(in srgb, var(--workspace-focus) 70%, transparent); outline-offset: 2px; }
.run-catalog { display: grid; align-content: start; gap: 4px; }
.run-catalog-row { display: grid; min-width: 0; gap: 3px; border: 0; border-radius: var(--workspace-radius-sm); background: transparent; padding: 8px; color: inherit; text-align: left; cursor: pointer; }
.run-catalog-row:hover { background: var(--workspace-surface-hover); }
.run-catalog-row.active { background: var(--workspace-surface-selected); }
.run-catalog-row span { overflow: hidden; color: var(--workspace-text); text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.run-catalog-row small { color: var(--workspace-text-muted); font-size: 11px; }
.console-tabs :deep(button) { font-size: 12px; }
.console-panel { padding: 14px 2px 16px 0; scrollbar-gutter: stable; }
.linear-trace { display: block; margin-top: 16px; border-top: 1px solid var(--workspace-border); padding-top: 0; }
.linear-trace > summary { display: list-item; position: relative; padding: 12px 0; cursor: pointer; color: var(--workspace-text-muted); font-size: 12px; }
.linear-trace > summary strong { margin-left: 3px; color: var(--workspace-text); font-size: 12px; font-weight: 600; }
.linear-trace > summary > span { float: right; color: var(--workspace-text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.trace-action strong { white-space: normal; line-height: 1.5; }
.trace-action small { white-space: normal; line-height: 1.5; }
.trace-action small span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.trace-index { border: 0; justify-content: start; font-size: 10px; font-weight: 400; }
.trace-row { align-items: start; padding: 10px 0; }
.trace-facts { padding-top: 3px; }
.audit-trace h3 { font-size: 12px; }
.overview-evidence-item .evidence-group-copy { display: grid; min-width: 0; gap: 5px; white-space: normal; }
.evidence-group-copy strong { font-size: 12px; }
.overview-evidence-item .evidence-group-copy small { font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.overview-evidence-item > svg { flex: none; color: var(--workspace-text-muted); }
.artifact-card { border: 0; border-bottom: 1px solid var(--workspace-border); border-radius: 0; background: transparent; padding: 4px 0; }
.inspection-backdrop { position: fixed; inset: 0; z-index: 120; background: var(--workspace-overlay-scrim); backdrop-filter: blur(6px); }
.inspection-dialog { position: fixed; top: 50%; left: 50%; z-index: 121; display: flex; flex-direction: column; width: min(1180px, calc(100vw - 48px)); max-height: calc(100dvh - 48px); transform: translate(-50%, -50%); border: 1px solid var(--workspace-border-strong); border-radius: 8px; background: var(--workspace-surface); color: var(--workspace-text); box-shadow: 0 24px 100px rgb(0 0 0 / 60%); overflow: hidden; }
.inspection-dialog-header { display: flex; flex: none; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 24px; border-bottom: 1px solid var(--workspace-border); }
.inspection-dialog-header :deep(h2) { margin: 0; font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.inspection-dialog-body { min-height: 0; min-width: 0; overflow: auto; padding: 24px; }
@media (max-width: 640px) { .inspection-dialog { width: calc(100vw - 16px); max-height: calc(100dvh - 16px); } .inspection-dialog-header { padding: 14px 16px; } .inspection-dialog-body { padding: 16px; } }
</style>
