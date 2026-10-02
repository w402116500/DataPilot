<script setup lang="ts">
import { ArrowUpRight } from "@lucide/vue";

import type { RunArtifact, SqlAudit, ToolCall, TraceDag, TraceDagEdge, TraceDagNode } from "@/api/types";
import { Button } from "@/components/ui/button";
import ToolCallResult from "@/components/ToolCallResult.vue";
import { traceActionLabel, traceNodeLabel } from "@/lib/traceDisplay";

const props = defineProps<{
  dag: TraceDag | null;
  node: TraceDagNode | null;
  tool?: ToolCall | null;
  audits?: readonly SqlAudit[];
  artifacts?: readonly RunArtifact[];
}>();

const emit = defineEmits<{
  openNode: [node: TraceDagNode];
}>();

function statusLabel(status: string | null): string {
  if (status === "succeeded" || status === "completed") return "已完成";
  if (status === "failed") return "未完成";
  if (status === "canceled" || status === "cancelled") return "已取消";
  if (status === "running") return "进行中";
  if (status === "queued") return "等待中";
  return status ?? "未记录";
}

function kindLabel(kind: TraceDagNode["kind"]): string {
  const labels: Record<TraceDagNode["kind"], string> = {
    "run-start": "运行开始",
    preparation: "准备阶段",
    "agent-turn": "分析轮次",
    tool: "工具调用",
    artifact: "产物",
    "final-answer": "最终答案",
    "run-terminal": "运行结果",
  };
  return labels[kind];
}

function actionStatusClass(status: string | null): string {
  if (status === "succeeded" || status === "completed") return "trace-detail-status-success";
  if (status === "failed") return "trace-detail-status-error";
  if (status === "running" || status === "queued") return "trace-detail-status-running";
  return "";
}

function isFailureStatus(status: string | null): boolean {
  return status === "failed" || status === "canceled" || status === "cancelled";
}

function actionReasonLabel(action: TraceDagNode["action_records"][number]): string | null {
  if (action.reason) return `原因：${action.reason}`;
  return isFailureStatus(action.status) ? "失败原因未提供" : null;
}

function formatDetailValue(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined || value === "") return "未记录";
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

function detailEntries(node: TraceDagNode): Array<{ label: string; value: string }> {
  const labels: Record<string, string> = {
    run_status: "运行状态",
    phase: "阶段",
    elapsed_ms: "耗时（毫秒）",
    failure_code: "失败码",
    action_kind: "动作类型",
    tool_names: "工具",
    tool_call_count: "工具数量",
    tool_name: "工具名",
    tool_status: "工具状态",
    evidence_count: "证据数量",
    error_code: "错误码",
    audit_status: "SQL 审计",
    row_count: "返回行数",
    artifact_type: "产物类型",
    mime_type: "MIME",
    size_bytes: "大小（字节）",
    inline_previewable: "可在线预览",
    attempt: "尝试次数",
    validation_stage: "校验阶段",
    answer_format: "答案格式",
    completion_kind: "完成类型",
    incomplete_reason: "未完成原因",
    artifact_count: "产物数",
  };
  return Object.entries(node.detail)
    .filter(([key, value]) => {
      if (labels[key] === undefined) return false;
      return !(key === "failure_code" || key === "error_code" || key === "incomplete_reason")
        || (value !== null && value !== undefined && value !== "");
    })
    .map(([key, value]) => ({ label: labels[key]!, value: formatDetailValue(value) }));
}

function edgeLabel(edge: TraceDagEdge): string {
  const kindLabels: Record<TraceDagEdge["kind"], string> = {
    starts: "开始",
    continues: "继续执行",
    invokes: "调用",
    produces_artifact: "生成产物",
    completes: "完成",
  };
  return kindLabels[edge.kind];
}

function nodeLabel(dag: TraceDag | null, nodeId: string): string {
  const node = dag?.nodes.find((candidate) => candidate.id === nodeId);
  return node ? traceNodeLabel(node) : "未解析节点";
}

function isActionable(node: TraceDagNode): boolean {
  return node.kind === "artifact";
}

function openArtifactNode(artifactId: string): void {
  const artifactNode = props.dag?.nodes.find((node) => node.artifact_id === artifactId);
  if (artifactNode) emit("openNode", artifactNode);
}
</script>

<template>
  <aside class="trace-node-details" aria-label="执行节点详情">
    <template v-if="props.node">
      <header class="trace-node-details-header">
        <div class="trace-node-details-heading">
          <span>{{ kindLabel(props.node.kind) }}</span>
          <h3 :title="props.node.label">{{ traceNodeLabel(props.node) }}</h3>
        </div>
        <span class="trace-node-details-status" :class="actionStatusClass(props.node.status)">
          {{ statusLabel(props.node.status) }}
        </span>
      </header>

      <div class="trace-node-details-scroll">
        <p v-if="props.node.summary" class="trace-node-details-summary">{{ props.node.summary }}</p>

        <dl class="trace-node-details-meta">
          <div v-if="props.node.start_seq !== null"><dt>起始事件</dt><dd>#{{ props.node.start_seq }}</dd></div>
          <div v-if="props.node.end_seq !== null"><dt>结束事件</dt><dd>#{{ props.node.end_seq }}</dd></div>
          <div v-if="props.node.relationship_status === 'unresolved'" class="trace-node-details-unresolved">
            <dt>关系</dt><dd>历史事件缺少可验证关联</dd>
          </div>
        </dl>

        <dl v-if="detailEntries(props.node).length" class="trace-node-details-facts">
          <div v-for="entry in detailEntries(props.node)" :key="entry.label">
            <dt>{{ entry.label }}</dt><dd>{{ entry.value }}</dd>
          </div>
        </dl>

        <section v-if="props.node.kind === 'agent-turn' && props.node.detail.reasoning" class="trace-node-details-section trace-node-details-reasoning">
          <header><strong>模型推理记录</strong><span>模型实际返回</span></header>
          <pre>{{ props.node.detail.reasoning }}</pre>
        </section>

        <section v-if="props.node.kind === 'agent-turn' && props.node.detail.assistant_output" class="trace-node-details-section trace-node-details-reasoning">
          <header><strong>模型回复</strong><span>模型实际返回</span></header>
          <pre>{{ props.node.detail.assistant_output }}</pre>
        </section>

        <section class="trace-node-details-section">
          <header><strong>动作记录</strong><span>{{ props.node.action_records.length }} 条</span></header>
          <ol v-if="props.node.action_records.length" class="trace-node-details-actions">
            <li v-for="action in props.node.action_records" :key="action.id">
              <div class="trace-node-details-action-topline">
                <code>#{{ action.event_seq }}</code>
                <strong :title="action.label">{{ traceActionLabel(action) }}</strong>
                <span :class="actionStatusClass(action.status)">{{ statusLabel(action.status) }}</span>
              </div>
              <p v-if="action.summary">{{ action.summary }}</p>
              <small
                v-if="actionReasonLabel(action)"
                class="trace-node-details-action-reason"
                :class="{ 'trace-node-details-action-reason-missing': isFailureStatus(action.status) && !action.reason }"
              >
                {{ actionReasonLabel(action) }}
              </small>
            </li>
          </ol>
          <p v-else class="trace-node-details-empty">该节点没有独立的持久化动作记录。</p>
        </section>

        <ToolCallResult
          v-if="props.node.kind === 'tool' && props.tool"
          :tool="props.tool"
          :audits="props.audits ?? []"
          :artifacts="props.artifacts ?? []"
          @open-artifact="openArtifactNode"
        />

        <section v-if="props.dag" class="trace-node-details-section">
          <header><strong>关联关系</strong><span>{{ props.node.relationship_status === "resolved" ? "已验证" : "部分未解析" }}</span></header>
          <div class="trace-node-details-edges">
            <div>
              <small>进入</small>
              <span v-for="edge in props.dag.edges.filter((candidate) => candidate.target === props.node?.id)" :key="edge.id" :title="edge.label ?? undefined">
                {{ edgeLabel(edge) }} · {{ nodeLabel(props.dag, edge.source) }}
              </span>
              <em v-if="!props.dag.edges.some((edge) => edge.target === props.node?.id)">暂无</em>
            </div>
            <div>
              <small>流出</small>
              <span v-for="edge in props.dag.edges.filter((candidate) => candidate.source === props.node?.id)" :key="edge.id" :title="edge.label ?? undefined">
                {{ edgeLabel(edge) }} · {{ nodeLabel(props.dag, edge.target) }}
              </span>
              <em v-if="!props.dag.edges.some((edge) => edge.source === props.node?.id)">暂无</em>
            </div>
          </div>
        </section>
      </div>

      <footer v-if="isActionable(props.node)" class="trace-node-details-footer">
        <Button
          v-if="isActionable(props.node)"
          variant="outline"
          size="sm"
          class="trace-node-details-open"
          @click="emit('openNode', props.node)"
        >
          <ArrowUpRight :size="14" aria-hidden="true" />
          {{ props.node.kind === "artifact" ? "打开产物页面" : "打开运行详情" }}
        </Button>
      </footer>
    </template>
    <div v-else class="trace-node-details-empty-state">
      <strong>尚未选择节点</strong>
      <span>节点的动作记录、关联关系和安全摘要会显示在这里。</span>
    </div>
  </aside>
</template>

<style scoped>
.trace-node-details { display: flex; min-width: 0; min-height: 0; flex-direction: column; overflow: hidden; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius); background: var(--workspace-surface); }.trace-node-details-header { display: flex; flex: none; align-items: flex-start; justify-content: space-between; gap: 10px; border-bottom: 1px solid var(--workspace-border); padding: 12px; }.trace-node-details-heading { display: grid; min-width: 0; gap: 3px; }.trace-node-details-heading > span, .trace-node-details-section header span, .trace-node-details-footer-note { color: var(--workspace-text-muted); font-size: 10px; }.trace-node-details-heading h3 { margin: 0; overflow-wrap: anywhere; color: var(--workspace-text); font-size: 13px; line-height: 1.35; }.trace-node-details-status { flex: none; color: var(--workspace-text-muted); font-size: 10px; white-space: nowrap; }.trace-detail-status-success { color: var(--workspace-state-success); }.trace-detail-status-error { color: var(--workspace-state-error); }.trace-detail-status-running { color: var(--workspace-state-running); }.trace-node-details-scroll { min-height: 0; flex: 1; overflow: auto; padding: 12px; }.trace-node-details-summary { margin: 0 0 12px; color: var(--workspace-text); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }.trace-node-details-meta, .trace-node-details-facts { display: grid; gap: 6px; margin: 0 0 12px; }.trace-node-details-meta div, .trace-node-details-facts div { display: grid; grid-template-columns: minmax(72px, .5fr) minmax(0, 1fr); gap: 8px; min-width: 0; }.trace-node-details-meta dt, .trace-node-details-facts dt { color: var(--workspace-text-muted); font-size: 10px; }.trace-node-details-meta dd, .trace-node-details-facts dd { min-width: 0; margin: 0; color: var(--workspace-text); font-size: 10px; overflow-wrap: anywhere; }.trace-node-details-unresolved dt, .trace-node-details-unresolved dd { color: var(--workspace-state-warning); }.trace-node-details-section { display: grid; gap: 7px; margin-top: 13px; border-top: 1px solid var(--workspace-border); padding-top: 11px; }.trace-node-details-section header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }.trace-node-details-section header strong { color: var(--workspace-text); font-size: 11px; }.trace-node-details-reasoning pre { max-height: 180px; margin: 0; overflow: auto; border: 1px solid var(--workspace-code-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-code-background); padding: 8px; color: var(--workspace-code-text); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 10px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }.trace-node-details-actions { display: grid; gap: 7px; margin: 0; padding: 0; list-style: none; }.trace-node-details-actions li { display: grid; gap: 3px; border-left: 2px solid var(--workspace-border-strong); padding-left: 8px; }.trace-node-details-action-topline { display: flex; min-width: 0; align-items: baseline; gap: 6px; }.trace-node-details-action-topline code { flex: none; color: var(--workspace-text-subtle); font-size: 9px; }.trace-node-details-action-topline strong { min-width: 0; flex: 1; overflow-wrap: anywhere; color: var(--workspace-text); font-size: 10px; }.trace-node-details-action-topline span { flex: none; font-size: 9px; }.trace-node-details-actions p, .trace-node-details-actions small, .trace-node-details-empty { margin: 0; color: var(--workspace-text-muted); font-size: 9px; line-height: 1.45; overflow-wrap: anywhere; }.trace-node-details-edges { display: grid; gap: 8px; }.trace-node-details-edges > div { display: grid; gap: 3px; min-width: 0; }.trace-node-details-edges small { color: var(--workspace-text-muted); font-size: 9px; }.trace-node-details-edges span, .trace-node-details-edges em { color: var(--workspace-text); font-size: 10px; font-style: normal; overflow-wrap: anywhere; }.trace-node-details-edges em { color: var(--workspace-text-subtle); }.trace-node-details-footer { display: flex; flex: none; align-items: center; border-top: 1px solid var(--workspace-border); padding: 10px 12px; }.trace-node-details-open { width: 100%; }.trace-node-details-empty-state { display: grid; min-height: 180px; place-items: center; align-content: center; gap: 6px; padding: 20px; text-align: center; }.trace-node-details-empty-state strong { color: var(--workspace-text); font-size: 12px; }.trace-node-details-empty-state span { max-width: 220px; color: var(--workspace-text-muted); font-size: 10px; line-height: 1.5; }
.trace-node-details-summary { margin-bottom: 14px; border-left: 2px solid var(--workspace-focus); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface-subtle); padding: 8px 9px; }
.trace-node-details-meta, .trace-node-details-facts { gap: 7px; }
.trace-node-details-meta dd, .trace-node-details-facts dd { color: var(--workspace-text); font-variant-numeric: tabular-nums; }
.trace-node-details-section { margin-top: 15px; }
.trace-node-details-section header strong { font-size: 12px; }
.trace-node-details-footer { background: var(--workspace-surface-subtle); }
.trace-node-details-actions small.trace-node-details-action-reason { border-left: 2px solid var(--workspace-border-strong); padding-left: 7px; }
.trace-node-details-actions small.trace-node-details-action-reason-missing { border-left-color: var(--workspace-state-warning); color: var(--workspace-state-warning); }
.trace-node-details-meta dt, .trace-node-details-facts dt,
.trace-node-details-meta dd, .trace-node-details-facts dd { font-size: 12px; line-height: 1.5; }
.trace-node-details-heading h3 { font-size: 14px; }
.trace-node-details-status { font-size: 11px; }
.trace-node-details-actions p, .trace-node-details-actions small, .trace-node-details-empty,
.trace-node-details-action-topline strong, .trace-node-details-reasoning pre,
.trace-node-details-edges span, .trace-node-details-edges em { font-size: 11px; line-height: 1.6; }
</style>
