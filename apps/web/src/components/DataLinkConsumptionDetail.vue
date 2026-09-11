<script setup lang="ts">
import type { DataLinkConsumption, DataLinkConsumptionList } from "@/api/types";
import DataLinkSemanticResult from "@/components/DataLinkSemanticResult.vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { X } from "@lucide/vue";

const props = defineProps<{
  open: boolean;
  list: DataLinkConsumptionList | null;
  selected: DataLinkConsumption | null;
}>();
const emit = defineEmits<{ close: []; select: [id: string] }>();

const stageLabel = { prepare: "准备阶段", tool: "工具阶段" } as const;
const payloadLabel = { complete: "完整记录", too_large: "载荷过大", unavailable: "当时不可用" } as const;
const returnedLabel = { ok: "有返回", no_match: "未命中字段", truncated: "结果被截断", unavailable: "不可用", too_large: "过大未保存" } as const;
const receiptLabel = { received: "已交给消费者", ignored_empty: "空结果未进入计划", not_received: "消费者未接收" } as const;

function formatTime(value: string): string {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleString("zh-CN") : value;
}
</script>

<template>
  <Dialog :open="open" @update:open="(value) => { if (!value) emit('close'); }">
    <DialogContent class="consumption-detail">
      <DialogHeader>
        <div class="detail-heading">
          <DialogTitle>DataLink 消费记录</DialogTitle>
          <Button variant="ghost" size="icon" aria-label="关闭消费详情" title="关闭消费详情" @click="emit('close')"><X :size="16" /></Button>
        </div>
        <DialogDescription>只表示系统向准备阶段或工具阶段传递了哪些安全语义，不表示模型推理采用了这些内容。</DialogDescription>
      </DialogHeader>
      <p v-if="list?.historical_status === 'missing'" class="panel-state">历史未记录完整内容，回放不会重新检索 DataLink。</p>
      <template v-else-if="list?.items.length">
        <div class="consumption-list">
          <button
            v-for="item in list.items"
            :key="item.id"
            type="button"
            class="consumption-row"
            :class="{ active: selected?.id === item.id }"
            @click="emit('select', item.id)"
          >
            <strong>{{ stageLabel[item.stage] }} · #{{ item.seq }}</strong>
            <small>{{ payloadLabel[item.payload_status] }} · {{ item.mode === "cached" ? "缓存" : item.mode === "live" ? "实时" : "未记录模式" }}</small>
          </button>
        </div>
        <dl v-if="selected" class="detail-metadata">
          <div><dt>检索问题</dt><dd>{{ selected.query }}</dd></div>
          <div><dt>阶段</dt><dd>{{ stageLabel[selected.stage] }}</dd></div>
          <div><dt>图谱版本</dt><dd>{{ selected.graph_version || "未记录" }}</dd></div>
          <div><dt>Schema</dt><dd>r{{ selected.schema_revision }}</dd></div>
          <div><dt>返回状态</dt><dd>{{ returnedLabel[selected.returned_status] }}</dd></div>
          <div><dt>消费状态</dt><dd>{{ receiptLabel[selected.consumer_receipt_status] }}</dd></div>
          <div><dt>截断</dt><dd>{{ selected.is_truncated ? "已截断" : "未截断" }}</dd></div>
          <div><dt>工具调用</dt><dd>{{ selected.tool_call_id || "准备阶段，无工具调用" }}</dd></div>
          <div><dt>记录时间</dt><dd>{{ formatTime(selected.created_at) }}</dd></div>
          <div><dt>摘要</dt><dd>{{ selected.summary.field_count }} 字段 · {{ selected.summary.relationship_count }} 关系 · {{ selected.summary.payload_bytes }} 字节</dd></div>
        </dl>
        <p v-if="selected?.payload_status === 'too_large'" class="panel-note">载荷超过 256 KiB，未截断保存不完整 JSON。</p>
        <p v-else-if="selected?.payload_status === 'unavailable'" class="panel-note">当时 DataLink 不可用，历史不补造检索结果。</p>
        <DataLinkSemanticResult v-else-if="selected?.semantic_context" :context="selected.semantic_context" />
        <p v-else-if="selected" class="panel-state">没有可展示的安全语义正文。</p>
      </template>
      <p v-else class="panel-state">本次 Run 没有 DataLink 消费记录。</p>
    </DialogContent>
  </Dialog>
</template>

<style scoped>
.detail-heading { display: flex; align-items: start; justify-content: space-between; gap: 14px; }
.detail-heading :deep(h2) { overflow-wrap: anywhere; font-size: 17px; }
.consumption-list { display: grid; gap: 8px; margin-bottom: 16px; }
.consumption-row { display: grid; gap: 4px; width: 100%; min-width: 0; border: 1px solid var(--workspace-border); border-radius: 8px; background: transparent; color: var(--workspace-text); padding: 10px 12px; text-align: left; cursor: pointer; }
.consumption-row.active, .consumption-row:hover { background: var(--workspace-surface-hover); }
.consumption-row small { color: var(--workspace-text-muted); font-size: 11px; overflow-wrap: anywhere; }
.detail-metadata { display: grid; gap: 12px; margin: 0 0 16px; font-size: 12px; }
.detail-metadata > div { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 12px; }
.detail-metadata dt { color: var(--workspace-text-muted); }
.detail-metadata dd { margin: 0; overflow-wrap: anywhere; line-height: 1.6; }
.panel-state, .panel-note { color: var(--workspace-text-muted); overflow-wrap: anywhere; }
.panel-note { color: var(--workspace-state-warning); }
:deep(.consumption-detail) { width: min(94vw, 760px); max-height: min(86vh, 820px); overflow: auto; }
@media (max-width: 560px) { .detail-metadata > div { grid-template-columns: minmax(0, 1fr); gap: 4px; } }
</style>
