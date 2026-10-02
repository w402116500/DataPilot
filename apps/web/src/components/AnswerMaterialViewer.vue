<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { DialogClose, DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from "reka-ui";
import { X } from "@lucide/vue";
import type { AnswerEvidenceSnapshot, RunArtifact, SqlAudit } from "@/api/types";
import ArtifactViewer from "@/components/ArtifactViewer.vue";
const props = defineProps<{ snapshot: AnswerEvidenceSnapshot | null; selectedNumber: number | null; artifacts: readonly RunArtifact[]; audits: readonly SqlAudit[] }>();
const emit = defineEmits<{ close: []; select: [number: number] }>();
const selected = computed(() => props.snapshot?.materials.find(item => item.number === props.selectedNumber));
const artifactSources = computed(() => selected.value?.sources.filter(source => source.kind === "artifact" && source.ref_id !== null) ?? []);
const artifactById = computed(() => new Map(props.artifacts.map(item => [item.id, item])));
const auditSources = computed(() => selected.value?.sources.filter(source => source.kind === "audit" && source.ref_id !== null) ?? []);
const auditById = computed(() => new Map(props.audits.map(item => [item.id, item])));
const expandedArtifactId = ref<string | null>(null);
watch(() => props.selectedNumber, () => { expandedArtifactId.value = null; });
</script>

<template>
  <DialogRoot :open="selectedNumber !== null && Boolean(selected)" @update:open="(open) => { if (!open) emit('close'); }">
    <DialogPortal>
      <DialogOverlay class="material-overlay" />
      <DialogContent class="material-dialog">
        <header>
          <DialogTitle>材料 [{{ selected?.number }}] · {{ selected?.title }}</DialogTitle>
          <DialogClose class="material-close" aria-label="关闭材料"><X :size="18" /></DialogClose>
        </header>
        <DialogDescription class="material-scope">{{ selected?.scope ?? "本答案保存的材料" }}</DialogDescription>
        <nav aria-label="本答案材料">
          <button v-for="item in snapshot?.materials" :key="item.number" type="button" :aria-current="item.number === selectedNumber ? 'true' : undefined" @click="emit('select', item.number)">[{{ item.number }}] {{ item.title }}</button>
        </nav>
        <pre class="material-content">{{ selected?.content }}</pre>
        <section v-if="selected?.sources.length" class="material-sources" aria-label="材料来源">
          <strong>来源</strong>
          <p v-for="(source, index) in selected.sources" :key="index">{{ source.label }}</p>
        </section>
        <section v-if="auditSources.length" class="material-audits" aria-label="SQL 审计摘要">
          <strong>查询审计</strong>
          <template v-for="source in auditSources" :key="source.ref_id">
            <dl v-if="source.ref_id && auditById.has(source.ref_id)">
              <div><dt>查询</dt><dd>第 {{ (auditById.get(source.ref_id)?.attempt_no ?? 0) + 1 }} 次</dd></div>
              <div><dt>状态</dt><dd>{{ auditById.get(source.ref_id)?.status }}</dd></div>
              <div><dt>关联表</dt><dd>{{ auditById.get(source.ref_id)?.referenced_tables.join("、") || "未记录" }}</dd></div>
              <div><dt>返回行数</dt><dd>{{ auditById.get(source.ref_id)?.row_count ?? "未记录" }}</dd></div>
            </dl>
            <p v-else class="material-scope">查询审计详情尚未加载或已不可用；保存的材料仍可查看。</p>
          </template>
        </section>
        <section v-if="artifactSources.length" class="material-artifacts" aria-label="产物详情">
          <template v-for="source in artifactSources" :key="source.ref_id">
            <button
              v-if="source.ref_id && artifactById.has(source.ref_id)"
              type="button"
              :aria-expanded="expandedArtifactId === source.ref_id"
              @click="expandedArtifactId = expandedArtifactId === source.ref_id ? null : source.ref_id"
            >{{ expandedArtifactId === source.ref_id ? "收起" : "查看" }}{{ source.label }}详情</button>
            <ArtifactViewer
              v-if="source.ref_id && expandedArtifactId === source.ref_id"
              :artifact="artifactById.get(source.ref_id) ?? null"
            />
            <p v-if="source.ref_id && !artifactById.has(source.ref_id)" class="material-scope">{{ source.label }}详情尚未加载或已不可用；保存的材料仍可查看。</p>
          </template>
        </section>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<style scoped>
.material-overlay { position: fixed; inset: 0; z-index: 80; background: rgb(0 0 0 / 45%); }
.material-dialog { position: fixed; z-index: 81; top: 50%; left: 50%; transform: translate(-50%, -50%); width: min(760px, calc(100vw - 24px)); max-height: calc(100dvh - 32px); overflow-y: auto; padding: 20px; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius); background: var(--workspace-surface); color: var(--workspace-text); overflow-wrap: anywhere; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
header h2 { margin: 0; font-size: 16px; }
.material-close { flex: none; border: 0; background: transparent; color: inherit; cursor: pointer; }
.material-scope { font-size: 12px; color: var(--workspace-text-muted); line-height: 1.6; }
nav { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
nav button { max-width: 100%; overflow-wrap: anywhere; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); padding: 6px 8px; background: transparent; color: inherit; font-size: 12px; text-align: left; cursor: pointer; }
nav button[aria-current="true"] { border-color: var(--workspace-focus); color: var(--workspace-focus); }
.material-content { white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.7; padding: 12px; background: var(--workspace-surface-inset); border-radius: var(--workspace-radius-sm); }
.material-sources { font-size: 12px; margin: 12px 0; }
.material-sources p { margin: 4px 0; }
.material-audits { margin-top: 12px; font-size: 12px; }
.material-audits dl { margin: 8px 0; border-top: 1px solid var(--workspace-border); }
.material-audits dl div { display: grid; grid-template-columns: 80px minmax(0, 1fr); gap: 8px; padding: 5px 0; }
.material-audits dt { color: var(--workspace-text-muted); }
.material-audits dd { margin: 0; overflow-wrap: anywhere; }
.material-artifacts { display: grid; gap: 8px; margin-top: 12px; }
.material-artifacts button { justify-self: start; min-height: 44px; border: 1px solid var(--workspace-border); border-radius: var(--workspace-radius-sm); background: var(--workspace-surface-subtle); color: var(--workspace-focus); padding: 8px 12px; cursor: pointer; }
.material-artifacts button:focus-visible, nav button:focus-visible, .material-close:focus-visible { outline: 2px solid var(--workspace-focus); outline-offset: 2px; }
@media (max-width: 680px) { .material-dialog { padding: 14px; } nav { max-height: 120px; overflow-y: auto; } nav button { min-height: 44px; } .material-close { min-width: 44px; min-height: 44px; } }
</style>
