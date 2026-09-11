<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Code2, FileText } from "@lucide/vue";
import ArtifactViewer from "@/components/ArtifactViewer.vue";
import ToolCallResult from "@/components/ToolCallResult.vue";
import type { RunInspectionGroup } from "@/lib/runInspection";

const props = defineProps<{ group: RunInspectionGroup; selectedArtifactId?: string }>();
const artifactId = ref("");
watch(() => [props.group.id, props.selectedArtifactId], () => {
  artifactId.value = props.group.artifacts.find((item) => item.id === props.selectedArtifactId)?.id
    ?? props.group.artifacts[0]?.id ?? "";
}, { immediate: true });
const artifact = computed(() => props.group.artifacts.find((item) => item.id === artifactId.value) ?? null);
const sql = computed(() => {
  const input = props.group.tool?.input_params?.sql;
  return props.group.audits[0]?.original_sql ?? (typeof input === "string" ? input : null);
});
const status = computed(() => {
  const value = props.group.tool?.status ?? props.group.audits[0]?.status;
  return ({ succeeded: "已完成", failed: "未完成", blocked: "已阻断", canceled: "已取消", running: "进行中", queued: "等待中" } as Record<string, string>)[value ?? ""] ?? "未记录";
});
function duration(milliseconds: number): string {
  if (milliseconds < 1000) return `${milliseconds} ms`;
  const seconds = Math.round(milliseconds / 100) / 10;
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${Number((seconds % 60).toFixed(1))} 秒`;
}
</script>

<template>
  <section class="inspection-content" :class="{ 'has-source': group.tool || group.audits.length }" aria-label="调用与结果">
    <aside v-if="group.tool || group.audits.length" class="inspection-source" aria-label="结果来源">
      <header><Code2 :size="16" /><strong>{{ group.title }}</strong><span>{{ status }}</span></header>
      <template v-if="sql !== null">
        <p v-if="group.tool?.error_code" class="inspection-error">{{ group.tool.error_message || `调用未完成：${group.tool.error_code}` }}</p>
        <h3>执行 SQL</h3>
        <pre class="inspection-sql"><code>{{ sql }}</code></pre>
        <dl v-for="audit in group.audits" :key="audit.id" class="inspection-facts">
          <div><dt>涉及表</dt><dd>{{ audit.referenced_tables.join("、") || "未引用表" }}</dd></div>
          <div><dt>返回行数</dt><dd>{{ audit.row_count ?? "未记录" }}</dd></div>
          <div v-if="audit.elapsed_ms !== null"><dt>耗时</dt><dd>{{ duration(audit.elapsed_ms) }}</dd></div>
          <div v-if="audit.statement_type"><dt>语句类型</dt><dd>{{ audit.statement_type }}</dd></div>
          <div v-if="audit.blocked_reason"><dt>审计说明</dt><dd>{{ audit.blocked_reason_code ? `${audit.blocked_reason_code}：` : "" }}{{ audit.blocked_reason }}</dd></div>
          <div v-if="audit.error_code"><dt>错误</dt><dd>{{ audit.error_code }}{{ audit.error_message ? `：${audit.error_message}` : "" }}</dd></div>
        </dl>
        <template v-for="audit in group.audits" :key="audit.id">
          <details v-if="audit.normalized_sql && audit.normalized_sql !== audit.original_sql" class="inspection-identifiers">
            <summary>归一化 SQL</summary>
            <pre class="inspection-sql"><code>{{ audit.normalized_sql }}</code></pre>
          </details>
        </template>
      </template>
      <ToolCallResult v-else-if="group.tool" :tool="group.tool" :audits="group.audits" :artifacts="[]" />
      <details class="inspection-identifiers">
        <summary>来源记录</summary>
        <dl class="inspection-facts">
          <div v-if="group.tool"><dt>调用 ID</dt><dd>{{ group.tool.id }}</dd></div>
          <div v-for="audit in group.audits" :key="audit.id"><dt>审计 ID</dt><dd>{{ audit.id }}</dd></div>
        </dl>
      </details>
    </aside>
    <section class="inspection-result" aria-label="关联查询结果与产物">
      <header><FileText :size="16" /><h3>{{ sql !== null ? "查询结果" : "关联产物" }}</h3><span>{{ group.artifacts.length }} 项</span></header>
      <label v-if="group.artifacts.length > 1" class="inspection-picker">产物
        <select v-model="artifactId" aria-label="选择关联产物">
          <option v-for="(item, index) in group.artifacts" :key="item.id" :value="item.id">{{ index + 1 }} · {{ item.title }} · {{ item.type }}</option>
        </select>
      </label>
      <p v-if="!group.tool && !group.audits.length" class="inspection-note">未记录关联调用。</p>
      <ArtifactViewer v-if="artifact" :key="artifact.id" :artifact="artifact" />
      <p v-else class="inspection-note">{{ status === "进行中" || status === "等待中" ? "正在等待本次调用的结果。" : "本次调用没有生成可查看的产物。" }}</p>
    </section>
  </section>
</template>

<style scoped>
.inspection-content { display: grid; min-width: 0; gap: 24px; }
.inspection-content.has-source { grid-template-columns: minmax(260px, 2fr) minmax(0, 3fr); }
.inspection-source, .inspection-result { min-width: 0; }
.inspection-source { border-right: 1px solid var(--workspace-border); padding-right: 24px; }
header { display: flex; align-items: center; gap: 8px; margin-bottom: 18px; }
header span { margin-left: auto; color: var(--workspace-text-muted); font-size: 12px; }
h3, header strong { margin: 0; font-size: 14px; font-weight: 600; }
.inspection-source > h3 { margin: 18px 0 10px; color: var(--workspace-text-muted); font-size: 12px; }
.inspection-sql { margin: 0; max-height: 360px; overflow: auto; padding: 16px; background: var(--workspace-code-background); border: 1px solid var(--workspace-border); border-radius: 6px; font-size: 13px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.inspection-facts { display: grid; gap: 12px; margin: 18px 0; font-size: 12px; }
.inspection-facts div { display: grid; grid-template-columns: 70px minmax(0, 1fr); gap: 12px; }
dt, .inspection-note { color: var(--workspace-text-muted); }
dd { margin: 0; overflow-wrap: anywhere; }
.inspection-note { font-size: 13px; line-height: 1.6; }
.inspection-error { color: var(--workspace-state-error); font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.inspection-identifiers { margin-top: 24px; color: var(--workspace-text-muted); font-size: 12px; }
summary { cursor: pointer; }
.inspection-identifiers > pre { margin-top: 12px; }
.inspection-picker { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; font-size: 12px; }
select { min-width: 0; flex: 1; padding: 8px; border: 1px solid var(--workspace-border); border-radius: 5px; background: var(--workspace-surface); color: var(--workspace-text); }
@media (max-width: 760px) { .inspection-content.has-source { grid-template-columns: minmax(0, 1fr); } .inspection-source { border-right: 0; border-bottom: 1px solid var(--workspace-border); padding: 0 0 20px; } }
</style>
