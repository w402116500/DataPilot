<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Activity, CheckCircle2, ChevronRight, Clock3, X, XCircle } from "@lucide/vue";
import { DialogClose, DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from "reka-ui";

import type { Run, RunArtifact, SqlAudit, ToolCall, TraceDag, TraceDagNode, TraceDagSection } from "@/api/types";
import { traceNodeLabel, traceSectionLabel } from "@/lib/traceDisplay";
import type { RunTraceEntry } from "@/lib/runTrace";
import { presentTraceEntries } from "@/lib/tracePresentation";
import RunTraceDag from "@/components/RunTraceDag.vue";
import TraceDagNodeDetails from "@/components/TraceDagNodeDetails.vue";
import { Button } from "@/components/ui/button";

const props = withDefaults(defineProps<{
  open: boolean;
  dag: TraceDag | null;
  loading?: boolean;
  error?: string | null;
  selectedNodeId?: string;
  run?: Run | null;
  traceEntries?: readonly RunTraceEntry[];
  artifacts?: readonly RunArtifact[];
  toolCalls?: readonly ToolCall[];
  sqlAudits?: readonly SqlAudit[];
}>(), {
  loading: false,
  error: null,
  selectedNodeId: "",
  run: null,
  traceEntries: () => [],
  artifacts: () => [],
  toolCalls: () => [],
  sqlAudits: () => [],
});

const emit = defineEmits<{
  "update:open": [open: boolean];
  selectNode: [node: TraceDagNode];
  openNode: [node: TraceDagNode];
  selectEntry: [entry: RunTraceEntry];
}>();

const selectedNode = computed(() => props.dag?.nodes.find((node) => node.id === props.selectedNodeId) ?? null);
const selectedSectionId = ref("");
const selectedSection = computed(() => props.dag?.sections.find((section) => section.id === selectedSectionId.value));
const sectionNodes = computed(() => props.dag?.nodes.filter((node) => selectedSection.value?.node_ids.includes(node.id)) ?? []);
watch([() => props.open, () => props.dag?.run_id, () => props.selectedNodeId], () => { selectedSectionId.value = ""; });
function selectNode(node: TraceDagNode): void {
  selectedSectionId.value = "";
  emit("selectNode", node);
}
function selectSection(section: TraceDagSection): void {
  selectedSectionId.value = section.id;
}
const selectedTool = computed(() => {
  const toolCallId = selectedNode.value?.tool_call_id;
  if (!toolCallId) return null;
  return props.toolCalls.find((tool) => tool.id === toolCallId) ?? null;
});
const selectedAudits = computed(() => selectedTool.value
  ? props.sqlAudits.filter((audit) => audit.tool_call_id === selectedTool.value?.id)
  : []);
const selectedArtifacts = computed(() => selectedTool.value
  ? props.artifacts.filter((artifact) => artifact.tool_call_id === selectedTool.value?.id
    || selectedAudits.value.some((audit) => audit.artifact_id === artifact.id))
  : props.artifacts);
const completedEntries = computed(() => props.traceEntries.filter((entry) => entry.status === "succeeded").length);
const toolEntries = computed(() => props.toolCalls.length);
const artifactEntries = computed(() => props.artifacts.length);
const presentedEntries = computed(() => presentTraceEntries(props.traceEntries, props.run?.id ?? props.dag?.run_id ?? ""));

function statusLabel(status: string | null | undefined): string {
  if (status === "succeeded" || status === "completed") return "已完成";
  if (status === "failed") return "未完成";
  if (status === "canceled" || status === "cancelled") return "已取消";
  if (status === "running") return "进行中";
  if (status === "queued") return "等待中";
  return status ?? "未记录";
}

function durationLabel(): string {
  if (!props.run?.started_at) return "未开始";
  const started = Date.parse(props.run.started_at);
  const finished = props.run.finished_at ? Date.parse(props.run.finished_at) : Date.now();
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return "未记录";
  const elapsed = Math.max(0, finished - started);
  if (elapsed >= 60_000) {
    const seconds = Math.round(elapsed / 100) / 10;
    return `${Math.floor(seconds / 60)} 分 ${Number((seconds % 60).toFixed(1))} 秒`;
  }
  return elapsed < 1_000 ? `${elapsed} ms` : `${(elapsed / 1_000).toFixed(1)} 秒`;
}
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="trace-overlay">
        <div class="trace-overlay-backdrop" aria-hidden="true" />
        <DialogContent as="section" class="trace-overlay-panel" aria-label="执行关系图">
          <header class="trace-overlay-header">
            <div class="trace-overlay-title">
              <DialogTitle class="trace-overlay-title-line"><Activity :size="16" aria-hidden="true" />执行关系图</DialogTitle>
              <DialogDescription class="sr-only">本次运行的分析轮次、工具调用和产物关系。</DialogDescription>
            </div>
            <div class="trace-overlay-header-side">
              <div class="trace-overlay-status" :class="`status-${run?.status ?? 'queued'}`">
                <CheckCircle2 v-if="run?.status === 'succeeded'" :size="14" aria-hidden="true" />
                <XCircle v-else-if="run?.status === 'failed' || run?.status === 'canceled'" :size="14" aria-hidden="true" />
                <Clock3 v-else :size="14" aria-hidden="true" />
                {{ statusLabel(run?.status) }}
              </div>
              <DialogClose as-child>
                <Button variant="ghost" size="icon-sm" title="关闭关系图">
                  <X :size="17" aria-hidden="true" /><span class="sr-only">关闭关系图</span>
                </Button>
              </DialogClose>
            </div>
          </header>

          <div class="trace-overlay-main trace-overlay-body">
            <RunTraceDag
              class="trace-overlay-graph"
              :dag="dag"
              :loading="loading"
              :error="error"
              :selected-node-id="selectedSection ? '' : selectedNodeId"
              mode="overlay"
              :show-action-records="false"
              @select-node="selectNode"
              @select-section="selectSection"
            />
            <aside v-if="selectedSection" class="trace-overlay-details trace-section-details" aria-label="阶段详情">
              <header><h3>{{ traceSectionLabel(selectedSection, dag?.nodes ?? []) }}</h3><span>{{ statusLabel(selectedSection.status) }}</span></header>
              <p>{{ sectionNodes.length }} 项 · 事件 #{{ selectedSection.start_seq }} 至 #{{ selectedSection.end_seq ?? selectedSection.start_seq }}</p>
              <ul>
                <li v-for="node in sectionNodes" :key="node.id"><button type="button" @click="selectNode(node)"><strong>{{ traceNodeLabel(node) }}</strong><span>{{ statusLabel(node.status) }} · #{{ node.start_seq }}</span></button></li>
              </ul>
            </aside>
            <TraceDagNodeDetails
              v-else
              class="trace-overlay-details"
              :dag="dag"
              :node="selectedNode"
              :tool="selectedTool"
              :audits="selectedAudits"
              :artifacts="selectedArtifacts"
              @open-node="emit('openNode', $event)"
            />
          </div>

          <details v-if="traceEntries.length" class="trace-overlay-list" aria-label="事件明细">
            <summary class="trace-overlay-list-heading"><ChevronRight :size="14" class="trace-list-chevron" aria-hidden="true" /><strong>事件明细</strong><span>{{ presentedEntries.length }} 项</span></summary>
            <ol class="trace-overlay-entries">
              <li v-for="entry in presentedEntries" :key="entry.id" :class="[`entry-${entry.status}`, { active: entry.startSeq <= (selectedNode?.detail_event_seq ?? -1) && entry.endSeq >= (selectedNode?.detail_event_seq ?? -1) }]">
                <button type="button" @click="selectedSectionId = ''; emit('selectEntry', entry)">
                  <span class="entry-seq">#{{ entry.startSeq }}<template v-if="entry.endSeq !== entry.startSeq">-{{ entry.endSeq }}</template></span>
                  <span class="entry-copy"><strong>{{ entry.title }}</strong><small>{{ entry.summary }}</small></span>
                  <span class="entry-status">{{ entry.statusLabel }}</span>
                </button>
              </li>
            </ol>
          </details>

          <footer class="trace-overlay-footer">
            <span>{{ traceEntries.length }} 条记录</span>
            <span>{{ toolEntries }} 次工具调用</span>
            <span>{{ artifactEntries }} 个产物</span>
            <span>{{ completedEntries }} 个已完成事件</span>
            <span class="trace-overlay-duration"><Clock3 :size="12" aria-hidden="true" />{{ durationLabel() }}</span>
          </footer>
        </DialogContent>
      </DialogOverlay>
    </DialogPortal>
  </DialogRoot>
</template>

<style scoped>
.trace-overlay { position: fixed; inset: 0; z-index: 60; display: grid; place-items: center; padding: 12px; }.trace-overlay-backdrop { position: absolute; inset: 0; background: var(--workspace-overlay-scrim); }.trace-overlay-panel { position: relative; display: flex; width: min(1500px, 100%); height: min(96dvh, 980px); min-height: 0; flex-direction: column; overflow: hidden; border: 1px solid var(--workspace-border-strong); border-radius: var(--workspace-radius); background: var(--workspace-surface); box-shadow: 0 24px 80px rgb(0 0 0 / 35%); outline: none; }.trace-overlay-header { display: flex; flex: none; align-items: flex-start; justify-content: space-between; gap: 20px; border-bottom: 1px solid var(--workspace-border); padding: 15px 18px 13px; }.trace-overlay-title { display: grid; min-width: 0; gap: 3px; }.trace-overlay-kicker { display: flex; align-items: center; gap: 6px; color: var(--workspace-focus); font-size: 9px; font-weight: 800; letter-spacing: .08em; }.trace-overlay-title h2 { margin: 0; color: var(--workspace-text); font-size: 17px; }.trace-overlay-title p { margin: 0; color: var(--workspace-text-muted); font-size: 10px; }.trace-overlay-header-side { display: flex; flex: none; align-items: center; gap: 12px; }.trace-overlay-status { display: inline-flex; align-items: center; gap: 5px; font-size: 10px; font-weight: 700; }.trace-overlay-status.status-succeeded { color: var(--workspace-state-success); }.trace-overlay-status.status-failed, .trace-overlay-status.status-canceled { color: var(--workspace-state-error); }.trace-overlay-status.status-running, .trace-overlay-status.status-queued { color: var(--workspace-state-running); }.trace-overlay-main { display: grid; min-height: 0; flex: 1 1 0; grid-template-columns: minmax(0, 1fr) minmax(300px, 360px); gap: 12px; overflow: hidden; background: var(--workspace-surface-subtle); padding: 12px 14px 14px; }.trace-overlay-graph { min-height: 0; min-width: 0; overflow: hidden; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius); background: var(--workspace-surface); padding: 12px 12px 0; }.trace-overlay-details { min-height: 0; box-shadow: 0 3px 12px color-mix(in srgb, var(--workspace-text) 7%, transparent); }.trace-overlay-list { display: grid; min-height: 112px; max-height: 190px; flex: none; grid-template-rows: auto minmax(0, 1fr); gap: 7px; overflow: hidden; border-top: 1px solid var(--workspace-border); background: var(--workspace-surface); padding: 11px 18px 9px; }.trace-overlay-list-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--workspace-border); padding-bottom: 7px; }.trace-overlay-list-heading strong { color: var(--workspace-text); font-size: 12px; }.trace-overlay-list-heading span { color: var(--workspace-text-muted); font-size: 9px; }.trace-overlay-entries { display: grid; min-height: 0; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 0 16px; margin: 0; overflow: auto; padding: 0; list-style: none; }.trace-overlay-entries li { min-width: 0; border-bottom: 1px solid var(--workspace-border); }.trace-overlay-entries button { display: grid; width: 100%; min-width: 0; grid-template-columns: 38px minmax(0, 1fr) auto; align-items: center; gap: 8px; border: 0; border-radius: var(--workspace-radius-sm); background: transparent; padding: 7px 5px; color: inherit; cursor: pointer; text-align: left; }.trace-overlay-entries button:hover, .trace-overlay-entries li.active button { background: var(--workspace-surface-selected); }.entry-seq { color: var(--workspace-text-subtle); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 9px; }.entry-copy { display: grid; min-width: 0; gap: 2px; }.entry-copy strong, .entry-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.entry-copy strong { color: var(--workspace-text); font-size: 11px; }.entry-copy small { color: var(--workspace-text-muted); font-size: 10px; }.entry-status { color: var(--workspace-text-muted); font-size: 9px; white-space: nowrap; }.entry-succeeded .entry-status { color: var(--workspace-state-success); }.entry-failed .entry-status { color: var(--workspace-state-error); }.entry-running .entry-status, .entry-queued .entry-status { color: var(--workspace-state-running); }.trace-overlay-empty { margin: 0; color: var(--workspace-text-muted); font-size: 10px; }.trace-overlay-footer { display: flex; flex: none; flex-wrap: wrap; gap: 6px; border-top: 1px solid var(--workspace-border); background: var(--workspace-surface-subtle); padding: 8px 18px; color: var(--workspace-text-muted); font-size: 9px; }.trace-overlay-footer > span:not(.trace-overlay-duration) { border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface); padding: 4px 7px; }.trace-overlay-duration { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; padding: 4px 0; }.trace-overlay-graph :deep(.trace-dag-overlay .trace-canvas-shell.is-overlay) { min-height: 0; }.trace-overlay-graph :deep(.trace-dag-heading strong) { font-size: 13px; }.trace-overlay-graph :deep(.trace-dag-heading span) { font-size: 10px; }.trace-overlay-details :deep(.trace-node-details-header) { background: var(--workspace-surface-subtle); }.trace-overlay-details :deep(.trace-node-details-footer) { background: var(--workspace-surface-subtle); }
@media (max-width: 860px) { .trace-overlay { padding: 0; }.trace-overlay-panel { width: 100%; height: 100dvh; border-radius: 0; }.trace-overlay-header { gap: 12px; padding: 13px 14px 11px; }.trace-overlay-main { grid-template-columns: minmax(0, 1fr); overflow: auto; padding: 10px; }.trace-overlay-graph { min-height: 470px; padding: 10px 10px 0; }.trace-overlay-details { min-height: 360px; }.trace-overlay-list { max-height: 260px; padding-inline: 12px; }.trace-overlay-footer { gap: 6px; padding-inline: 12px; }.trace-overlay-duration { margin-left: 0; } }
@media (max-width: 560px) { .trace-overlay-title p { max-width: 230px; line-height: 1.45; }.trace-overlay-entries { grid-template-columns: minmax(0, 1fr); }.trace-overlay-entries button { grid-template-columns: 34px minmax(0, 1fr) auto; }.trace-overlay-graph :deep(.trace-dag-heading strong) { font-size: 12px; } }
.trace-overlay-title-line { display: flex; align-items: center; gap: 8px; }
.trace-overlay-title-line svg { color: var(--workspace-text-muted); }
.trace-overlay-list-heading span { font-size: 11px; }
.trace-overlay-list { display: block; min-height: 0; max-height: none; padding: 0; }
.trace-overlay-list-heading { display: flex; min-height: 40px; align-items: center; justify-content: flex-start; border-bottom: 0; padding: 10px 18px; cursor: pointer; }
.trace-overlay-list-heading::-webkit-details-marker { display: none; }
.trace-overlay-list-heading span { margin-left: auto; }
.trace-list-chevron { color: var(--workspace-text-muted); }
.trace-overlay-list[open] .trace-list-chevron { transform: rotate(90deg); }
.trace-overlay-entries { max-height: 150px; padding: 0 18px 10px; }
.trace-overlay-entries button { grid-template-columns: 64px minmax(0, 1fr) auto; }
.entry-seq { overflow-wrap: anywhere; }
.trace-section-details { padding: 18px; overflow: auto; }
.trace-section-details header { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
.trace-section-details h3 { margin: 0; color: var(--workspace-text); font-size: 14px; }
.trace-section-details header span, .trace-section-details p { color: var(--workspace-text-muted); font-size: 12px; }
.trace-section-details ul { margin: 16px 0 0; padding: 0; list-style: none; }
.trace-section-details button { display: grid; gap: 6px; width: 100%; border: 0; border-bottom: 1px solid var(--workspace-border); padding: 12px 0; background: transparent; color: var(--workspace-text); cursor: pointer; text-align: left; }
.trace-section-details button strong { font-size: 13px; overflow-wrap: anywhere; }
.trace-section-details button span { color: var(--workspace-text-muted); font-size: 11px; }
.trace-section-details button:hover, .trace-section-details button:focus-visible { background: var(--workspace-surface-selected); }
</style>
