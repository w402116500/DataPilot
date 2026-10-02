<script setup lang="ts">
import type { DataLinkValidation } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { X } from "@lucide/vue";

const props = defineProps<{ validation: DataLinkValidation | null }>();
const emit = defineEmits<{ close: [] }>();

const statusLabel: Record<DataLinkValidation["status"], string> = {
  running: "进行中",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
  canceled: "已取消",
  interrupted: "已中断",
};

function metric(value: number | boolean | null | undefined): string {
  if (value === null || value === undefined) return "未记录";
  if (typeof value === "boolean") return value ? "存在" : "未发现";
  return String(value);
}

function formatTime(value: string | null): string {
  if (!value) return "未结束";
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleString("zh-CN") : value;
}
</script>

<template>
  <Dialog :open="validation !== null" @update:open="(open) => { if (!open) emit('close'); }">
    <DialogContent class="validation-detail">
      <DialogHeader>
        <div class="detail-heading">
          <DialogTitle>关系核验详情</DialogTitle>
          <Button variant="ghost" size="icon" aria-label="关闭核验详情" title="关闭核验详情" @click="emit('close')"><X :size="16" /></Button>
        </div>
        <DialogDescription>数据检查通过不等于业务关系正确。核验不会发布图谱。</DialogDescription>
      </DialogHeader>
      <dl v-if="validation" class="detail-metadata">
        <div><dt>状态</dt><dd :class="`status-${validation.status}`">{{ statusLabel[validation.status] }}{{ validation.expired ? " · 已过期" : "" }}</dd></div>
        <div><dt>检查方向</dt><dd>{{ validation.direction === "source_to_target" ? "源 → 目标" : "目标 → 源" }}</dd></div>
        <div><dt>图谱版本</dt><dd>{{ validation.graph_version }}</dd></div>
        <div><dt>Schema</dt><dd>r{{ validation.schema_revision }}</dd></div>
        <div><dt>端点指纹</dt><dd>{{ validation.endpoint_fingerprint }}</dd></div>
        <div><dt>开始时间</dt><dd>{{ formatTime(validation.created_at) }}</dd></div>
        <div><dt>结束时间</dt><dd>{{ formatTime(validation.finished_at) }}</dd></div>
        <div><dt>源非空</dt><dd>{{ metric(validation.source_non_null_count) }}</dd></div>
        <div><dt>目标非空</dt><dd>{{ metric(validation.target_non_null_count) }}</dd></div>
        <div><dt>源不同值</dt><dd>{{ metric(validation.source_distinct_count) }}</dd></div>
        <div><dt>目标不同值</dt><dd>{{ metric(validation.target_distinct_count) }}</dd></div>
        <div><dt>目标重复键</dt><dd>{{ metric(validation.target_duplicate_count) }}</dd></div>
        <div><dt>源未匹配</dt><dd>{{ metric(validation.source_unmatched_count) }}</dd></div>
        <div><dt>多重匹配风险</dt><dd>{{ metric(validation.multiple_match_risk) }}</dd></div>
        <div><dt>SQL 审计</dt><dd>{{ validation.audit_log_ids.join("、") || "无" }}</dd></div>
        <div><dt>表格产物</dt><dd>{{ validation.artifact_ids.join("、") || "无" }}</dd></div>
        <div v-if="validation.error_code"><dt>错误码</dt><dd>{{ validation.error_code }}</dd></div>
      </dl>
    </DialogContent>
  </Dialog>
</template>

<style scoped>
.detail-heading { display: flex; align-items: start; justify-content: space-between; gap: 14px; }
.detail-heading :deep(h2) { overflow-wrap: anywhere; font-size: 17px; }
.detail-metadata { display: grid; gap: 12px; margin: 0; font-size: 12px; }
.detail-metadata > div { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 12px; }
.detail-metadata dt { color: var(--workspace-text-muted); }
.detail-metadata dd { margin: 0; overflow-wrap: anywhere; line-height: 1.6; }
.status-running { color: var(--workspace-state-running); }
.status-completed { color: var(--workspace-state-succeeded); }
.status-partial { color: var(--workspace-state-warning); }
.status-failed, .status-canceled, .status-interrupted { color: var(--workspace-state-error); }
:deep(.validation-detail) { width: min(92vw, 640px); }
@media (max-width: 560px) { .detail-metadata > div { grid-template-columns: 80px minmax(0, 1fr); } }
</style>
