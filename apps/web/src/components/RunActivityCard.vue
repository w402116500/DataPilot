<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  Bot,
  CheckCircle2,
  ChevronDown,
  Database,
  FileText,
  LoaderCircle,
  XCircle,
} from "@lucide/vue";

import type { RunArtifact, SqlAudit, ToolCall } from "@/api/types";
import ToolCallResult from "@/components/ToolCallResult.vue";
import type { RunActivity, RunActivityItem, RunActivityStep } from "@/lib/runActivity";
import { isTerminalRunStatus } from "@/stores/runProjection";

const props = withDefaults(defineProps<{
  activity: RunActivity;
  defaultExpanded?: boolean;
  toolCalls?: readonly ToolCall[];
  sqlAudits?: readonly SqlAudit[];
  artifacts?: readonly RunArtifact[];
}>(), {
  defaultExpanded: false,
  toolCalls: () => [],
  sqlAudits: () => [],
  artifacts: () => [],
});

const emit = defineEmits<{
  openDetail: [item: RunActivityItem];
  openArtifact: [artifactId: string];
}>();

const terminal = computed(() => isTerminalRunStatus(props.activity.status));
const expanded = ref(defaultActivityExpanded());
const expandedSteps = ref<Record<string, boolean>>({});

function resetExpanded(): void {
  expanded.value = defaultActivityExpanded();
  expandedSteps.value = {};
}

function defaultActivityExpanded(): boolean {
  return props.defaultExpanded || !terminal.value || props.activity.status === "failed" ||
    props.activity.status === "canceled" || props.activity.completionKind === "partial" ||
    props.activity.completionKind === "clarification";
}

function activityStatusClass(): string {
  return props.activity.completionKind === "partial" || props.activity.completionKind === "clarification"
    ? "status-partial"
    : `status-${props.activity.status ?? "unknown"}`;
}

function statusLabel(status: RunActivityItem["status"]): string {
  if (status === "running") return "进行中";
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "未完成";
  if (status === "canceled") return "已取消";
  return "等待中";
}

function stepStatusLabel(status: RunActivityStep["status"]): string {
  return statusLabel(status);
}

function sameCopy(left: string | null | undefined, right: string | null | undefined): boolean {
  if (left === null || left === undefined || right === null || right === undefined) return false;
  const first = left.trim();
  const second = right.trim();
  if (first.length === 0 || second.length === 0) return false;
  return first === second || first.endsWith(second) || second.endsWith(first);
}

function stepCaption(step: RunActivityStep): string | null {
  const status = stepStatusLabel(step.status);
  const detail = step.detail?.trim() ?? "";
  if (detail.length > 0 && detail !== status && !sameCopy(detail, step.result)) return detail;
  if (step.toolCallIds.length > 0) return `${step.toolCallIds.length} 个工具`;
  return null;
}

function showStepResult(step: RunActivityStep): boolean {
  const result = step.result?.trim() ?? "";
  if (result.length === 0) return false;
  return result !== stepStatusLabel(step.status);
}

function itemCaption(item: RunActivityItem): string | null {
  const detail = item.detail?.trim() ?? "";
  if (detail.length === 0 || detail === statusLabel(item.status)) return null;
  return detail;
}

function isStepExpanded(step: RunActivityStep): boolean {
  return expandedSteps.value[step.id] ?? (
    step.status === "running" || step.status === "failed" || step.status === "canceled"
  );
}

function toggleStep(step: RunActivityStep): void {
  expandedSteps.value = { ...expandedSteps.value, [step.id]: !isStepExpanded(step) };
}

function openStep(step: RunActivityStep): void {
  const firstItem = step.items[0];
  if (firstItem !== undefined) emit("openDetail", firstItem);
}

function toolFor(item: RunActivityItem): ToolCall | null {
  return props.toolCalls.find((tool) => tool.id === item.toolCallId) ?? null;
}

function auditsFor(item: RunActivityItem): SqlAudit[] {
  return props.sqlAudits.filter((audit) => audit.tool_call_id === item.toolCallId);
}

function artifactsFor(item: RunActivityItem): RunArtifact[] {
  const auditArtifactIds = new Set(
    auditsFor(item).flatMap((audit) => audit.artifact_id === null ? [] : [audit.artifact_id]),
  );
  return props.artifacts.filter(
    (artifact) => artifact.tool_call_id === item.toolCallId || auditArtifactIds.has(artifact.id),
  );
}

function toolRowClass(item: RunActivityItem): string {
  const name = toolFor(item)?.tool_name;
  if (name === "run_sql_readonly") return "tool-sql";
  if (name === "run_python") return "tool-python";
  if (name === "explore_datalink") return "tool-datalink";
  if (name === undefined) return "";
  return "tool-schema";
}

watch(() => props.activity.runId, resetExpanded);
watch(
  () => props.activity.status,
  (status, previousStatus) => {
    if (isTerminalRunStatus(status) && !isTerminalRunStatus(previousStatus)) {
      expanded.value = defaultActivityExpanded();
      expandedSteps.value = {};
    }
  },
);
</script>

<template>
  <section class="run-activity" :class="{ 'is-expanded': expanded }" :aria-label="`Run ${activity.runId} 的分析过程`">
    <button
      class="activity-summary"
      type="button"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <span class="activity-summary-icon" :class="activityStatusClass()">
        <LoaderCircle v-if="!terminal" :size="15" aria-hidden="true" />
        <CheckCircle2 v-else-if="activity.status === 'succeeded'" :size="15" aria-hidden="true" />
        <XCircle v-else :size="15" aria-hidden="true" />
      </span>
      <span class="activity-summary-content">
        <strong>{{ activity.summary }}</strong>
        <span>{{ activity.toolCount }} 个工具{{ activity.artifactCount ? `，${activity.artifactCount} 个产物` : "" }}</span>
      </span>
      <ChevronDown class="activity-chevron" :class="{ 'is-open': expanded }" :size="16" aria-hidden="true" />
    </button>

    <ol v-if="expanded" class="activity-list">
      <template v-for="entry in activity.displayEntries" :key="entry.id">
      <li v-if="entry.kind === 'step'" class="activity-step-group" :class="`activity-${entry.step.status}`">
        <span class="activity-rail" aria-hidden="true">
          <span class="activity-step-number">
            <LoaderCircle v-if="entry.step.status === 'running'" :size="12" />
            <template v-else>{{ entry.stepNumber }}</template>
          </span>
          <span class="activity-step-line" />
        </span>
        <div class="activity-body">
          <div class="activity-heading">
            <button class="activity-step-title" type="button" @click="openStep(entry.step)">
              {{ entry.step.title }}
            </button>
            <span class="activity-status" :class="`status-${entry.step.status}`">{{ stepStatusLabel(entry.step.status) }}</span>
          </div>
          <p v-if="stepCaption(entry.step)" class="activity-caption">{{ stepCaption(entry.step) }}</p>
          <dl class="activity-step-summary">
            <div>
              <dt>目标</dt>
              <dd>{{ entry.step.goal }}</dd>
            </div>
            <div>
              <dt>动作</dt>
              <dd>{{ entry.step.action }}</dd>
            </div>
            <div v-if="showStepResult(entry.step)" :class="{ 'is-error': entry.step.status === 'failed' }">
              <dt>结果</dt>
              <dd>{{ entry.step.result }}</dd>
            </div>
          </dl>
          <div v-if="isStepExpanded(entry.step)" class="activity-tool-list">
            <div
              v-for="item in entry.step.items"
              :key="item.id"
              class="activity-tool-row"
              :class="toolRowClass(item)"
            >
              <button type="button" class="activity-tool-open" @click="emit('openDetail', item)">
                <span><Database :size="12" aria-hidden="true" />{{ item.title }}</span>
                <small>{{ item.detail ?? statusLabel(item.status) }}</small>
              </button>
              <ToolCallResult
                embedded
                :tool="toolFor(item)"
                :audits="auditsFor(item)"
                :artifacts="artifactsFor(item)"
                @open-artifact="emit('openArtifact', $event)"
              />
            </div>
          </div>
          <button class="activity-expand" type="button" :aria-expanded="isStepExpanded(entry.step)" @click="toggleStep(entry.step)">
            <ChevronDown class="activity-expand-icon" :class="{ 'is-open': isStepExpanded(entry.step) }" :size="12" aria-hidden="true" />
            {{ entry.step.toolCallIds.length === 0 ? "本轮无工具调用" : isStepExpanded(entry.step) ? "收起调用" : `查看 ${entry.step.toolCallIds.length} 个调用` }}
          </button>
        </div>
      </li>
      <li v-else class="activity-row" :class="`activity-${entry.item.status}`">
        <span class="activity-rail" aria-hidden="true">
          <span class="activity-step-dot" />
          <span class="activity-step-line" />
        </span>
        <button class="activity-item-open" type="button" @click="entry.item.kind === 'artifact' && entry.item.artifactId !== null ? emit('openArtifact', entry.item.artifactId) : emit('openDetail', entry.item)">
          <span class="activity-heading">
            <span class="activity-item-title">
              <Bot v-if="entry.item.kind === 'analysis' || entry.item.kind === 'answer'" :size="13" aria-hidden="true" />
              <FileText v-else :size="13" aria-hidden="true" />
              {{ entry.item.title }}
            </span>
            <span class="activity-status" :class="`status-${entry.item.status}`">{{ statusLabel(entry.item.status) }}</span>
          </span>
          <small v-if="itemCaption(entry.item)">{{ itemCaption(entry.item) }}</small>
        </button>
      </li>
      </template>
      <li v-if="activity.displayEntries.length === 0" class="activity-empty">过程记录会在分析开始后显示。</li>
    </ol>
  </section>
</template>

<style scoped>
.run-activity {
  display: grid;
  width: min(100%, 760px);
  min-width: 0;
  height: max-content;
  grid-auto-rows: max-content;
  border: 1px solid var(--workspace-border);
  border-radius: var(--workspace-radius);
  background: var(--workspace-surface);
  color: var(--workspace-text);
}

.activity-summary {
  display: grid;
  width: 100%;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  border: 0;
  background: transparent;
  padding: 11px 14px;
  border-radius: var(--workspace-radius);
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.run-activity.is-expanded .activity-summary {
  border-bottom-right-radius: 0;
  border-bottom-left-radius: 0;
}

.activity-summary:hover {
  background: var(--workspace-surface-hover);
}

.activity-summary:active {
  transform: translateY(1px);
}

.activity-summary:focus-visible,
.activity-step-title:focus-visible,
.activity-tool-open:focus-visible,
.activity-expand:focus-visible,
.activity-item-open:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--workspace-focus) 70%, transparent);
  outline-offset: 2px;
}

.activity-summary-icon {
  display: grid;
  width: 22px;
  height: 22px;
  place-items: center;
  color: var(--workspace-text-muted);
}

.activity-summary-icon.status-running,
.activity-summary-icon.status-queued {
  color: var(--workspace-state-running);
}

.activity-summary-icon.status-succeeded {
  color: var(--workspace-state-success);
}

.activity-summary-icon.status-partial {
  color: var(--workspace-state-warning);
}

.activity-summary-icon.status-failed {
  color: var(--workspace-state-error);
}

.activity-summary-icon.status-canceled {
  color: var(--workspace-state-canceled);
}

.activity-summary-content {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.activity-summary-content strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 700;
}

.activity-summary-content span {
  color: var(--workspace-text-muted);
  font-size: 11px;
  line-height: 1.35;
}

.activity-chevron,
.activity-expand-icon {
  flex: none;
  color: var(--workspace-text-subtle);
  transition: transform 0.15s ease;
}

.activity-chevron.is-open,
.activity-expand-icon.is-open {
  transform: rotate(180deg);
}

.activity-list {
  display: grid;
  margin: 0;
  border-top: 1px solid var(--workspace-border);
  padding: 4px 0 6px;
  list-style: none;
}

.activity-row,
.activity-step-group {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr);
  align-items: start;
  gap: 10px;
  min-width: 0;
  padding: 10px 14px 12px;
}

.activity-rail {
  position: relative;
  display: grid;
  height: 100%;
  min-height: 22px;
  place-items: start center;
}

.activity-step-number {
  z-index: 1;
  display: grid;
  width: 22px;
  height: 22px;
  place-items: center;
  border: 1px solid var(--workspace-border-strong);
  border-radius: 50%;
  background: var(--workspace-surface);
  color: var(--workspace-text-muted);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
}

.activity-step-dot {
  z-index: 1;
  width: 8px;
  height: 8px;
  margin-top: 7px;
  border-radius: 50%;
  background: var(--workspace-text-subtle);
}

.activity-step-line {
  position: absolute;
  top: 24px;
  bottom: -12px;
  width: 1px;
  background: var(--workspace-border);
}

.activity-list > li:last-child .activity-step-line {
  display: none;
}

.activity-running .activity-step-number,
.activity-queued .activity-step-number {
  border-color: color-mix(in srgb, var(--workspace-state-running) 50%, var(--workspace-border));
  color: var(--workspace-state-running);
}

.activity-succeeded .activity-step-number {
  border-color: color-mix(in srgb, var(--workspace-state-success) 50%, var(--workspace-border));
  color: var(--workspace-state-success);
}

.activity-failed .activity-step-number {
  border-color: color-mix(in srgb, var(--workspace-state-error) 50%, var(--workspace-border));
  color: var(--workspace-state-error);
}

.activity-canceled .activity-step-number {
  border-color: color-mix(in srgb, var(--workspace-state-canceled) 50%, var(--workspace-border));
  color: var(--workspace-state-canceled);
}

.activity-running .activity-step-dot,
.activity-queued .activity-step-dot {
  background: var(--workspace-state-running);
}

.activity-succeeded .activity-step-dot {
  background: var(--workspace-state-success);
}

.activity-failed .activity-step-dot {
  background: var(--workspace-state-error);
}

.activity-canceled .activity-step-dot {
  background: var(--workspace-state-canceled);
}

.activity-body,
.activity-item-open {
  display: grid;
  min-width: 0;
  gap: 6px;
}

.activity-item-open {
  border: 0;
  background: transparent;
  padding: 0;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.activity-heading {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
}

.activity-step-title,
.activity-item-title {
  min-width: 0;
  overflow-wrap: break-word;
  color: var(--workspace-text);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
}

.activity-step-title {
  border: 0;
  background: transparent;
  padding: 0;
  cursor: pointer;
  text-align: left;
}

.activity-step-title:hover,
.activity-item-open:hover .activity-item-title {
  color: var(--workspace-focus);
}

.activity-item-title {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
}

.activity-item-title :deep(svg) {
  flex: none;
  color: var(--workspace-text-muted);
}

.activity-status {
  flex: none;
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 2px 7px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.2;
}

.activity-status.status-running,
.activity-status.status-waiting {
  background: color-mix(in srgb, var(--workspace-state-running) 18%, transparent);
  color: var(--workspace-state-running);
}

.activity-status.status-succeeded {
  background: color-mix(in srgb, var(--workspace-state-success) 18%, transparent);
  color: var(--workspace-state-success);
}

.activity-status.status-failed {
  background: color-mix(in srgb, var(--workspace-state-error) 18%, transparent);
  color: var(--workspace-state-error);
}

.activity-status.status-canceled {
  background: color-mix(in srgb, var(--workspace-state-canceled) 18%, transparent);
  color: var(--workspace-state-canceled);
}

.activity-caption,
.activity-item-open small {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--workspace-text-muted);
  font-size: 11px;
  line-height: 1.45;
}

.activity-step-summary {
  display: grid;
  gap: 4px;
  margin: 0;
}

.activity-step-summary div {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr);
  gap: 8px;
  min-width: 0;
}

.activity-step-summary dt,
.activity-step-summary dd {
  min-width: 0;
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.activity-step-summary dt {
  color: var(--workspace-text-subtle);
}

.activity-step-summary dd {
  color: var(--workspace-text-muted);
}

.activity-step-summary .is-error dt,
.activity-step-summary .is-error dd {
  color: var(--workspace-state-error);
}

.activity-tool-list {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.activity-tool-row {
  display: grid;
  min-width: 0;
  gap: 8px;
  border: 1px solid var(--workspace-border);
  border-radius: var(--workspace-radius);
  background: var(--workspace-surface-subtle);
  padding: 8px 10px;
}

.activity-tool-row.tool-sql {
  border-left: 2px solid var(--workspace-tool-sql);
}

.activity-tool-row.tool-python {
  border-left: 2px solid var(--workspace-tool-python);
}

.activity-tool-row.tool-datalink {
  border-left: 2px solid var(--workspace-tool-datalink);
}

.activity-tool-row.tool-schema {
  border-left: 2px solid var(--workspace-tool-schema);
}

.activity-tool-open {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border: 0;
  background: transparent;
  padding: 0;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.activity-tool-open:hover {
  color: var(--workspace-focus);
}

.activity-tool-open span {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
  overflow: hidden;
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-tool-open small {
  flex: none;
  color: var(--workspace-text-subtle);
  font-size: 11px;
}

.activity-expand {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  justify-self: start;
  border: 0;
  background: transparent;
  padding: 2px 0;
  color: var(--workspace-focus);
  cursor: pointer;
  font-size: 11px;
}

.activity-empty {
  padding: 10px 14px;
  color: var(--workspace-text-muted);
  font-size: 12px;
  line-height: 1.45;
}

@media (prefers-reduced-motion: no-preference) {
  .activity-summary-icon.status-running :deep(svg),
  .activity-summary-icon.status-queued :deep(svg),
  .activity-running .activity-step-number :deep(svg) {
    animation: activity-spin 1.25s linear infinite;
  }
}

@media (prefers-reduced-motion: reduce) {
  .activity-chevron,
  .activity-expand-icon {
    transition: none;
  }
}

@keyframes activity-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 680px) {
  .activity-summary,
  .activity-row,
  .activity-step-group {
    padding-right: 12px;
    padding-left: 12px;
  }

  .activity-step-title,
  .activity-item-title {
    font-size: 12px;
  }
}
</style>
