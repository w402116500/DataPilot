<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  ChevronDown,
  ChevronRight,
  Clock3,
  Code2,
  Database,
  FileText,
  Network,
  Rows3,
  Table2,
  Terminal,
  XCircle,
} from "@lucide/vue";

import type { RunArtifact, SqlAudit, ToolCall } from "@/api/types";
import ArtifactViewer from "@/components/ArtifactViewer.vue";
import { formatSqlAuditLabel } from "@/lib/evidenceResolver";
import { toolLabel } from "@/lib/runActivity";

const props = withDefaults(defineProps<{
  tool: ToolCall | null;
  audits?: readonly SqlAudit[];
  artifacts?: readonly RunArtifact[];
  defaultExpanded?: boolean;
  embedded?: boolean;
}>(), {
  audits: () => [],
  artifacts: () => [],
  defaultExpanded: true,
  embedded: false,
});

const emit = defineEmits<{
  openArtifact: [artifactId: string];
}>();

const HIDDEN_SUMMARY_KEYS = new Set([
  "stdout",
  "stderr",
  "audit_log_id",
  "artifact_id",
  "execution_status",
  "discovery_observation",
]);

const expanded = ref(props.defaultExpanded);
const inputParams = computed(() => props.tool?.input_params ?? null);
const safeSummary = computed(() => props.tool?.output_summary === null || props.tool?.output_summary === undefined
  ? []
  : Object.entries(props.tool.output_summary));
const plainSummary = computed(() => safeSummary.value.filter(([key]) => !HIDDEN_SUMMARY_KEYS.has(key)));
const stdout = computed(() => summaryString("stdout"));
const stderr = computed(() => summaryString("stderr"));
const hasDetails = computed(
  () => inputParams.value !== null || safeSummary.value.length > 0 || props.audits.length > 0 || props.artifacts.length > 0,
);

function statusLabel(status: string | null | undefined): string {
  if (status === "running") return "进行中";
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "未完成";
  if (status === "canceled") return "已取消";
  if (status === "queued") return "等待中";
  return "未记录";
}

function formatDuration(elapsedMs: number | null | undefined): string | null {
  if (elapsedMs === null || elapsedMs === undefined) return null;
  if (elapsedMs < 1_000) return `${elapsedMs} ms`;
  return `${(elapsedMs / 1_000).toFixed(1)} 秒`;
}

function summaryLabel(key: string): string {
  const labels: Record<string, string> = {
    row_count: "返回行数",
    elapsed_ms: "耗时",
    evidence_count: "证据数",
    result_count: "结果数",
    graph_version: "图谱版本",
    node_count: "字段数",
    edge_count: "物理关系数",
    node_names: "涉及字段",
    relationships: "物理关系路径",
    semantic_entity_count: "语义实体数",
    semantic_mapping_count: "字段实体关联数",
    semantic_entities: "语义实体",
    exit_code: "退出码",
    stdout: "标准输出",
    stderr: "错误输出",
    output_count: "产物数",
    sandbox_status: "沙盒状态",
  };
  return labels[key] ?? key;
}

function inputString(key: string): string | null {
  const value = inputParams.value?.[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function inputNumber(key: string): number | null {
  const value = inputParams.value?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function inputStringList(key: string): string[] {
  const value = inputParams.value?.[key];
  return Array.isArray(value) ? value : [];
}

function summaryString(key: string): string | null {
  const value = props.tool?.output_summary?.[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function artifactLabel(type: RunArtifact["type"]): string {
  const labels: Record<RunArtifact["type"], string> = {
    table: "数据表",
    chart: "图表",
    markdown: "分析说明",
    file: "文件",
  };
  return labels[type];
}

function toolToneClass(toolName: ToolCall["tool_name"]): string {
  if (toolName === "run_sql_readonly") return "tool-sql";
  if (toolName === "run_python") return "tool-python";
  if (toolName === "explore_datalink") return "tool-datalink";
  return "tool-schema";
}

watch(
  () => props.tool?.id,
  () => { expanded.value = props.defaultExpanded; },
);
</script>

<template>
  <section v-if="tool" class="tool-call-result" :class="[toolToneClass(tool.tool_name), `tool-status-${tool.status}`, { 'is-expanded': expanded || embedded, 'is-embedded': embedded }]">
    <header v-if="!embedded" class="tool-call-heading">
      <span class="tool-call-name"><Database :size="13" aria-hidden="true" />{{ toolLabel(tool.tool_name) }}</span>
      <span class="tool-call-status" :class="`status-${tool.status}`">{{ statusLabel(tool.status) }}</span>
    </header>

    <p v-if="tool.error_code" class="tool-call-error"><XCircle :size="13" aria-hidden="true" />{{ tool.error_message || `调用未完成：${tool.error_code}` }}</p>

    <template v-if="hasDetails">
      <button
        v-if="!embedded"
        type="button"
        class="tool-result-toggle"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span><Table2 :size="13" aria-hidden="true" />查看本次调用详情</span>
        <ChevronDown v-if="expanded" :size="14" aria-hidden="true" />
        <ChevronRight v-else :size="14" aria-hidden="true" />
      </button>

      <div v-if="embedded || expanded" class="tool-result-content">
        <section v-if="tool.tool_name === 'run_sql_readonly' && inputString('sql')" class="tool-input tool-input-sql">
          <div class="tool-input-title"><Code2 :size="13" aria-hidden="true" />执行 SQL</div>
          <pre><code>{{ inputString("sql") }}</code></pre>
        </section>

        <section v-else-if="tool.tool_name === 'run_python' && inputParams" class="tool-input tool-input-python">
          <div class="tool-input-title"><Terminal :size="13" aria-hidden="true" />Python 分析</div>
          <dl v-if="inputString('purpose') || inputStringList('output_paths').length" class="tool-input-meta">
            <div v-if="inputString('purpose')"><dt>目的</dt><dd>{{ inputString("purpose") }}</dd></div>
            <div v-if="inputStringList('output_paths').length"><dt>声明产物</dt><dd>{{ inputStringList("output_paths").join("、") }}</dd></div>
          </dl>
          <pre v-if="inputString('script')"><code>{{ inputString("script") }}</code></pre>
        </section>

        <section v-else-if="tool.tool_name === 'explore_datalink' && inputParams" class="tool-input tool-input-datalink">
          <div class="tool-input-title"><Network :size="13" aria-hidden="true" />关系探索条件</div>
          <dl class="tool-input-meta">
            <div v-if="inputString('query')"><dt>问题</dt><dd>{{ inputString("query") }}</dd></div>
            <div v-if="inputString('focus')"><dt>关注点</dt><dd>{{ inputString("focus") }}</dd></div>
            <div v-if="inputNumber('max_nodes') !== null"><dt>节点上限</dt><dd>{{ inputNumber("max_nodes") }}</dd></div>
          </dl>
        </section>

        <div v-if="audits.length" class="tool-audits">
          <section v-for="audit in audits" :key="audit.id" class="tool-audit">
            <div class="tool-audit-title"><span>{{ formatSqlAuditLabel(audit.attempt_no) }}</span><small>{{ statusLabel(audit.status) }}</small></div>
            <dl>
              <div v-if="audit.referenced_tables.length"><dt>涉及表</dt><dd>{{ audit.referenced_tables.join("、") }}</dd></div>
              <div v-if="audit.row_count !== null"><dt>返回行数</dt><dd><Rows3 :size="12" aria-hidden="true" />{{ audit.row_count }}</dd></div>
              <div v-if="formatDuration(audit.elapsed_ms)"><dt>耗时</dt><dd><Clock3 :size="12" aria-hidden="true" />{{ formatDuration(audit.elapsed_ms) }}</dd></div>
              <div v-if="audit.blocked_reason"><dt>审计结论</dt><dd>{{ audit.blocked_reason }}</dd></div>
            </dl>
          </section>
        </div>

        <dl v-if="plainSummary.length" class="tool-summary">
          <template v-for="[key, value] in plainSummary" :key="key"><dt>{{ summaryLabel(key) }}</dt><dd>{{ value ?? "-" }}</dd></template>
        </dl>

        <section v-if="stdout || stderr" class="tool-streams">
          <div v-if="stdout"><span>标准输出</span><pre>{{ stdout }}</pre></div>
          <div v-if="stderr"><span>错误输出</span><pre class="is-error">{{ stderr }}</pre></div>
        </section>

        <section v-if="artifacts.length" class="tool-artifacts" aria-label="本次调用产生的结果">
          <div v-for="artifact in artifacts" :key="artifact.id" class="tool-artifact">
            <button type="button" class="tool-artifact-heading" @click="emit('openArtifact', artifact.id)">
              <span><FileText :size="13" aria-hidden="true" />{{ artifact.title }}</span>
              <small>{{ artifactLabel(artifact.type) }} · 打开完整结果</small>
            </button>
            <ArtifactViewer :artifact="artifact" :max-rows="5" compact hide-download />
          </div>
        </section>
      </div>
    </template>
    <p v-else-if="tool.status === 'succeeded'" class="tool-result-empty">本次调用没有生成可展示的受控结果。</p>
  </section>
</template>

<style scoped>
.tool-call-result { display: grid; gap: 7px; min-width: 0; border-left: 2px solid var(--workspace-border-strong); padding: 7px 0 7px 9px; }.tool-call-result.tool-sql { border-left-color: var(--workspace-tool-sql); }.tool-call-result.tool-python { border-left-color: var(--workspace-tool-python); }.tool-call-result.tool-datalink { border-left-color: var(--workspace-tool-datalink); }.tool-call-result.tool-schema { border-left-color: var(--workspace-tool-schema); }.tool-call-result.is-embedded { gap: 6px; border-left: 0; padding: 0; }
.tool-call-heading, .tool-call-name, .tool-result-toggle, .tool-result-toggle span, .tool-call-error, .tool-artifact-heading span, .tool-audit-title, .tool-audit dd, .tool-input-title { display: flex; align-items: center; }.tool-call-heading { justify-content: space-between; gap: 8px; }.tool-call-name { min-width: 0; gap: 5px; color: var(--workspace-text); font-size: 12px; font-weight: 700; }.tool-call-status { flex: none; color: var(--workspace-text-muted); font-size: 10px; }.tool-call-status.status-running, .tool-call-status.status-queued { color: var(--workspace-state-running); }.tool-call-status.status-succeeded { color: var(--workspace-state-success); }.tool-call-status.status-failed { color: var(--workspace-state-error); }.tool-call-status.status-canceled { color: var(--workspace-state-canceled); }
.tool-result-toggle, .tool-artifact-heading { width: 100%; justify-content: space-between; gap: 7px; border: 0; background: transparent; padding: 0; color: var(--workspace-focus); cursor: pointer; text-align: left; }.tool-result-toggle { font-size: 11px; font-weight: 600; }.tool-result-toggle span { gap: 5px; }.tool-result-content { display: grid; gap: 8px; }.tool-input { display: grid; min-width: 0; gap: 6px; }.tool-input-title { gap: 5px; color: var(--workspace-text-muted); font-size: 10px; font-weight: 700; }.tool-sql .tool-input-title { color: var(--workspace-tool-sql); }.tool-python .tool-input-title { color: var(--workspace-tool-python); }.tool-datalink .tool-input-title { color: var(--workspace-tool-datalink); }
.tool-input pre, .tool-streams pre { max-height: 220px; margin: 0; overflow: auto; border: 1px solid var(--workspace-code-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-code-background); padding: 8px 10px; color: var(--workspace-code-text); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }.tool-input-meta { display: grid; gap: 4px; margin: 0; }.tool-input-meta div { display: grid; grid-template-columns: 58px minmax(0, 1fr); gap: 7px; }.tool-input-meta dt, .tool-input-meta dd { min-width: 0; margin: 0; font-size: 10px; overflow-wrap: anywhere; }.tool-input-meta dt { color: var(--workspace-text-muted); }.tool-input-meta dd { color: var(--workspace-text); }.tool-audits { display: grid; gap: 5px; }.tool-audit { border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface-subtle); padding: 7px 8px; }.tool-audit-title { justify-content: space-between; gap: 8px; color: var(--workspace-text); font-size: 11px; font-weight: 700; }.tool-audit-title small { color: var(--workspace-text-muted); font-size: 10px; font-weight: 500; }.tool-audit dl, .tool-summary { display: grid; gap: 4px 8px; margin: 6px 0 0; }.tool-audit dl { grid-template-columns: 58px minmax(0, 1fr); }.tool-audit dl div { display: contents; }.tool-audit dt, .tool-summary dt { color: var(--workspace-text-muted); font-size: 10px; }.tool-audit dd, .tool-summary dd { min-width: 0; gap: 4px; margin: 0; color: var(--workspace-text); font-size: 10px; overflow-wrap: anywhere; }.tool-summary { grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface-subtle); padding: 7px 8px; }.tool-streams { display: grid; gap: 7px; }.tool-streams div { display: grid; gap: 4px; }.tool-streams span { color: var(--workspace-text-muted); font-size: 10px; font-weight: 700; }.tool-streams pre { border-color: var(--workspace-code-inverse-border); background: var(--workspace-code-inverse-background); color: var(--workspace-code-inverse-text); }.tool-streams pre.is-error { border-color: var(--workspace-code-error-border); background: var(--workspace-code-error-background); color: var(--workspace-code-error-text); }.tool-artifacts { display: grid; gap: 9px; }.tool-artifact { display: grid; gap: 6px; }.tool-artifact-heading { color: var(--workspace-tool-artifact); }.tool-artifact-heading span { min-width: 0; gap: 5px; overflow: hidden; font-size: 11px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }.tool-artifact-heading small { flex: none; color: var(--workspace-text-muted); font-size: 10px; }.tool-call-error { gap: 5px; margin: 0; color: var(--workspace-state-error); font-size: 11px; line-height: 1.45; }.tool-result-empty { margin: 0; color: var(--workspace-text-muted); font-size: 11px; line-height: 1.45; }
</style>
