<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ArrowLeft, ArrowRight, RefreshCw, RotateCcw, GitMerge, ListTree, X } from "@lucide/vue";
import { getDatalinkVersionCatalog, getDatalinkVersionConflicts, getDatalinkVersionDiff, getDatalinkVersions, resolveDatalinkCandidate, restoreDatalinkVersion } from "@/api/datasources";
import { ApiClientError } from "@/api/client";
import type { DataLinkAutomaticSemantics, DataLinkCatalogItem, DataLinkConflictResolution, DataLinkNodeType, DataLinkRebuildConflict, DataLinkRebuildConflicts, DataLinkVersion, DataLinkVersionDiff, DataLinkVersionDiffRelation, DataLinkVersions } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { datalinkNodeName, datalinkProvenanceLabels, datalinkRelationLabels } from "@/lib/datalinkDisplay";

const props = defineProps<{ datasourceId: string; graphVersion: string; schemaRevision: number; hasUnsavedChanges: boolean }>();
const emit = defineEmits<{ published: [] }>();
const versions = ref<DataLinkVersions | null>(null);
const page = ref(1);
const loading = ref(false);
const error = ref("");
const selected = ref<DataLinkVersion | null>(null);
const inspectMode = ref<"restore" | "conflicts" | "diff">("restore");
const conflicts = ref<DataLinkRebuildConflicts | null>(null);
const diff = ref<DataLinkVersionDiff | null>(null);
const catalog = ref<Partial<Record<DataLinkNodeType, DataLinkCatalogItem[]>>>({});
const choices = ref<Record<string, { action: "" | "discard" | "rebind"; node: string; source: string; target: string }>>({});
const confirmed = ref(false);
const detailLoading = ref(false);
const detailReady = ref(false);
const detailError = ref("");
const busy = ref(false);
let listGeneration = 0;
let detailGeneration = 0;
let listController: AbortController | null = null;
let detailController: AbortController | null = null;
let operationKey: string | null = null;
const labels = { automated: "自动构建", manual: "人工修订", restore: "恢复配置" };
const stateLabels = { running: "构建中", completed: "已完成", failed: "失败" };

const pageCount = computed(() => Math.max(1, Math.ceil((versions.value?.total ?? 0) / 20)));
const isCandidate = computed(() => selected.value?.publication_state === "candidate");
const viewingDiff = computed(() => inspectMode.value === "diff");
const diffLabels: Record<NonNullable<DataLinkVersionDiff>["items"][number]["kind"], string> = {
  node_added: "新增语义对象",
  node_removed: "移除语义对象",
  node_updated: "修改业务语义",
  relation_added: "新增关系",
  relation_removed: "移除关系",
  relation_updated: "修订关系",
  mapping_replaced: "重配业务映射",
};
const canSubmit = computed(() => {
  if (viewingDiff.value || !selected.value || busy.value || detailLoading.value || !detailReady.value || !confirmed.value || props.hasUnsavedChanges || selected.value.schema_revision !== props.schemaRevision) return false;
  if (!isCandidate.value) return selected.value.status === "completed" && !selected.value.is_head;
  return !!conflicts.value?.items.length && conflicts.value.items.every((item) => {
    const choice = choices.value[item.object_key];
    if (!choice?.action) return false;
    if (choice.action === "discard") return true;
    const kind = item.object_kind === "relation" ? "column" : item.node_type;
    if (!kind) return false;
    const ids = new Set((catalog.value[kind] ?? []).map((entry) => entry.node.id));
    if (item.object_kind === "node") return ids.has(choice.node);
    const source = catalog.value.column?.find((entry) => entry.node.id === choice.source)?.node;
    const target = catalog.value.column?.find((entry) => entry.node.id === choice.target)?.node;
    return !!source?.table && !!target?.table && source.table !== target.table;
  });
});

function message(caught: unknown): string {
  return caught instanceof ApiClientError ? `${caught.message}（${caught.code}，请求 ${caught.requestId}）` : "无法完成版本操作，请重试。";
}
async function load(): Promise<void> {
  listController?.abort(); listController = new AbortController();
  const signal = listController.signal;
  const ticket = ++listGeneration;
  loading.value = true; error.value = "";
  try {
    const result = await getDatalinkVersions(props.datasourceId, page.value, signal);
    if (ticket !== listGeneration) return;
    if (result.datasource_id !== props.datasourceId) throw new Error("Version identity mismatch");
    versions.value = result;
  } catch (caught) { if (ticket === listGeneration && !signal.aborted) error.value = message(caught); }
  finally { if (ticket === listGeneration) loading.value = false; }
}
function close(force = false): void {
  if (busy.value && !force) return;
  detailGeneration += 1; detailController?.abort(); detailController = null; selected.value = null; conflicts.value = null; diff.value = null; inspectMode.value = "restore"; choices.value = {}; confirmed.value = false; detailError.value = ""; detailLoading.value = false; detailReady.value = false; busy.value = false; operationKey = null;
}
async function inspect(version: DataLinkVersion): Promise<void> {
  close(); selected.value = version; inspectMode.value = version.publication_state === "candidate" ? "conflicts" : "restore";
  if (version.publication_state !== "candidate") { detailReady.value = true; return; }
  detailController = new AbortController(); const signal = detailController.signal;
  const ticket = detailGeneration;
  detailLoading.value = true;
  catalog.value = {};
  try {
    const result = await getDatalinkVersionConflicts(props.datasourceId, version.graph_version, signal);
    if (ticket !== detailGeneration) return;
    if (result.datasource_id !== props.datasourceId || result.candidate_graph_version !== version.graph_version || result.schema_revision !== props.schemaRevision || result.base_graph_version !== props.graphVersion) throw new ApiClientError({ code: "DATALINK_HEAD_STALE", message: "候选版本的基线已变化，请刷新版本列表。", details: {} }, "local");
    conflicts.value = result;
    choices.value = Object.fromEntries(result.items.map((item) => [item.object_key, { action: "", node: "", source: "", target: "" }]));
    const kinds = new Set(result.items.map((item) => item.object_kind === "relation" ? "column" : item.node_type).filter((kind): kind is DataLinkNodeType => kind !== null));
    for (const kind of kinds) {
      const items: DataLinkCatalogItem[] = [];
      let catalogPage = 1;
      while (true) {
        const response = await getDatalinkVersionCatalog(props.datasourceId, version.graph_version, kind, catalogPage, signal);
        if (ticket !== detailGeneration) return;
        if (response.datasource_id !== props.datasourceId || response.graph_version !== version.graph_version) throw new Error("Candidate catalog mismatch");
        items.push(...response.items);
        if (items.length >= response.total || !response.items.length) break;
        catalogPage += 1;
      }
      catalog.value[kind] = items;
    }
    detailReady.value = true;
  } catch (caught) { if (ticket === detailGeneration && !signal.aborted) detailError.value = message(caught); }
  finally { if (ticket === detailGeneration) detailLoading.value = false; }
}
async function inspectDiff(version: DataLinkVersion): Promise<void> {
  close(); selected.value = version; inspectMode.value = "diff";
  detailController = new AbortController(); const signal = detailController.signal;
  const ticket = detailGeneration;
  detailLoading.value = true;
  try {
    const result = await getDatalinkVersionDiff(props.datasourceId, version.graph_version, signal);
    if (ticket !== detailGeneration) return;
    if (result.datasource_id !== props.datasourceId || result.graph_version !== version.graph_version) throw new Error("Version identity mismatch");
    diff.value = result;
    detailReady.value = true;
  } catch (caught) { if (ticket === detailGeneration && !signal.aborted) detailError.value = message(caught); }
  finally { if (ticket === detailGeneration) detailLoading.value = false; }
}
function semanticsText(value: DataLinkAutomaticSemantics | null | undefined): string {
  if (!value) return "无";
  return [value.name || "未命名", value.description || "说明留空", value.aliases.length ? value.aliases.join("、") : "无别名", value.semantic_type || "无语义类型"].join(" · ");
}
function relationText(relation: DataLinkVersionDiffRelation | null | undefined): string {
  if (!relation) return "无";
  return `${relation.source_name} → ${relation.target_name} · ${relation.enabled ? "启用" : "禁用"} · ${datalinkRelationLabels[relation.type]} · ${datalinkProvenanceLabels[relation.provenance]}`;
}
function mappingText(targets: string[]): string {
  return targets.length ? targets.join("、") : "无映射";
}
function options(item: DataLinkRebuildConflict): DataLinkCatalogItem[] {
  const kind = item.object_kind === "relation" ? "column" : item.node_type;
  return kind ? catalog.value[kind] ?? [] : [];
}
async function submit(): Promise<void> {
  if (!canSubmit.value || !selected.value) return;
  detailController ??= new AbortController();
  const signal = detailController.signal; const ticket = detailGeneration;
  busy.value = true; detailError.value = ""; operationKey ??= crypto.randomUUID();
  try {
    const common = { expected_head: props.graphVersion, schema_revision: props.schemaRevision, idempotency_key: operationKey };
    if (isCandidate.value && conflicts.value) {
      const resolutions: DataLinkConflictResolution[] = conflicts.value.items.map((item) => {
        const choice = choices.value[item.object_key];
        if (choice.action === "discard") return { object_key: item.object_key, action: "discard" };
        return item.object_kind === "node" ? { object_key: item.object_key, action: "rebind", node_id: choice.node } : { object_key: item.object_key, action: "rebind", source_id: choice.source, target_id: choice.target };
      });
      await resolveDatalinkCandidate(props.datasourceId, { ...common, candidate_graph_version: selected.value.graph_version, resolutions }, signal);
    } else {
      await restoreDatalinkVersion(props.datasourceId, { ...common, target_graph_version: selected.value.graph_version }, signal);
    }
    if (ticket !== detailGeneration) return;
    busy.value = false; close(); emit("published");
  } catch (caught) { if (ticket === detailGeneration && !signal.aborted) detailError.value = message(caught); }
  finally { if (ticket === detailGeneration) busy.value = false; }
}
function turnPage(delta: number): void { if (loading.value || page.value + delta < 1 || page.value + delta > pageCount.value) return; page.value += delta; void load(); }
watch(choices, () => { operationKey = null; confirmed.value = false; }, { deep: true, flush: "sync" });
watch([() => props.datasourceId, () => props.graphVersion, () => props.schemaRevision], () => { close(true); page.value = 1; versions.value = null; void load(); }, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { listGeneration += 1; listController?.abort(); close(true); });
</script>

<template>
  <section class="version-history" aria-label="DataLink 版本历史">
    <div class="history-toolbar"><span>{{ versions?.total ?? 0 }} 个版本</span><Button variant="ghost" size="icon" :disabled="loading" title="刷新版本" aria-label="刷新版本" @click="load"><RefreshCw :size="15" /></Button></div>
    <p v-if="hasUnsavedChanges" class="history-notice">存在未保存修改，保存草稿后再恢复或处理冲突。</p>
    <p v-if="error" role="alert" class="history-error">{{ error }}</p>
    <p v-else-if="loading" role="status">正在读取版本…</p>
    <p v-else-if="!versions?.items.length" class="history-notice">暂无版本记录</p>
    <div v-for="version in versions?.items ?? []" v-else :key="version.graph_version" class="version-row">
      <div><strong>{{ labels[version.origin_kind] }} <span v-if="version.is_head">· 当前生效</span></strong><small>{{ version.graph_version }}</small><p>Schema {{ version.schema_revision }} · {{ stateLabels[version.status] }} · {{ version.publication_state === 'published' ? '已发布' : '候选版本' }}{{ version.conflict_count ? ` · ${version.conflict_count} 项冲突` : '' }}</p><small>{{ new Date(version.created_at).toLocaleString() }}</small></div>
      <div class="version-actions">
        <Button v-if="version.status === 'completed'" size="sm" variant="outline" @click="inspectDiff(version)"><ListTree :size="14" />查看差异</Button>
        <Button v-if="version.publication_state === 'candidate' && version.conflict_count" size="sm" variant="outline" :disabled="hasUnsavedChanges || version.schema_revision !== schemaRevision || version.status !== 'completed'" @click="inspect(version)"><GitMerge :size="14" />处理冲突</Button>
        <Button v-else-if="!version.is_head && version.publication_state === 'published' && version.status === 'completed'" size="sm" variant="outline" :disabled="hasUnsavedChanges || version.schema_revision !== schemaRevision" @click="inspect(version)"><RotateCcw :size="14" />恢复配置</Button>
      </div>
      <span v-if="version.schema_revision !== schemaRevision" class="history-notice">Schema 不兼容</span>
    </div>
    <div class="history-pagination"><Button variant="ghost" size="icon" :disabled="loading || page <= 1" title="上一页版本" aria-label="上一页版本" @click="turnPage(-1)"><ArrowLeft :size="15" /></Button><span>{{ page }} / {{ pageCount }}</span><Button variant="ghost" size="icon" :disabled="loading || page >= pageCount" title="下一页版本" aria-label="下一页版本" @click="turnPage(1)"><ArrowRight :size="15" /></Button></div>
    <Dialog :open="!!selected" @update:open="(value) => { if (!value) close(); }"><DialogContent class="sm:max-w-[760px]"><DialogHeader><div class="history-heading"><DialogTitle>{{ viewingDiff ? '版本语义差异' : isCandidate ? '处理重建冲突' : '恢复历史配置' }}</DialogTitle><Button variant="ghost" size="icon" :disabled="busy" title="关闭版本操作" aria-label="关闭版本操作" @click="close()"><X :size="16" /></Button></div><DialogDescription class="version-identity">{{ selected?.graph_version }}</DialogDescription></DialogHeader>
      <p v-if="detailError" role="alert" class="history-error">{{ detailError }}</p>
      <Button v-if="detailError && selected && viewingDiff" variant="outline" :disabled="busy" @click="inspectDiff(selected)">重新读取差异</Button>
      <Button v-else-if="detailError && selected && isCandidate" variant="outline" :disabled="busy" @click="inspect(selected)">重新读取冲突</Button>
      <p v-if="detailLoading" role="status">{{ viewingDiff ? '正在读取语义差异…' : '正在读取冲突与可绑定对象…' }}</p>
      <template v-else-if="viewingDiff && diff">
        <p class="history-notice">对比基线：{{ diff.base_graph_version || '无' }}</p>
        <p v-if="diff.truncated" class="history-notice">差异条目已达到上限，仅展示前 500 项。</p>
        <p v-if="!diff.items.length" class="history-notice">{{ diff.base_graph_version ? '相对基线没有可展示的语义差异' : '该版本没有可对比的基线' }}</p>
        <div v-for="item in diff.items" :key="`${item.kind}:${item.object_key}`" class="conflict-row">
          <strong>{{ diffLabels[item.kind] }} · {{ item.name }}</strong>
          <p v-if="item.object_kind === 'node'" class="history-notice">自动值：{{ semanticsText(item.automatic) }}<br />当前值：{{ semanticsText(item.effective) }}</p>
          <p v-else-if="item.object_kind === 'relation'" class="history-notice">修订前：{{ relationText(item.before_relation) }}<br />修订后：{{ relationText(item.after_relation) }}</p>
          <p v-else class="history-notice">修订前：{{ mappingText(item.before_targets) }}<br />修订后：{{ mappingText(item.after_targets) }}</p>
        </div>
      </template>
      <template v-else-if="isCandidate && conflicts"><div v-for="item in conflicts.items" :key="item.object_key" class="conflict-row"><strong>{{ item.name }}</strong><p class="history-notice">{{ item.reason }}</p><label>处理方式<select v-model="choices[item.object_key].action" :aria-label="`${item.name}处理方式`" :disabled="busy"><option value="" disabled>请选择</option><option value="discard">放弃此项人工修订</option><option value="rebind">重新绑定并保留修订</option></select></label><template v-if="choices[item.object_key].action === 'rebind'"><label v-if="item.object_kind === 'node'">绑定对象<select v-model="choices[item.object_key].node" :aria-label="`${item.name}绑定对象`" :disabled="busy"><option value="" disabled>选择同类对象</option><option v-for="entry in options(item)" :key="entry.node.id" :value="entry.node.id">{{ datalinkNodeName(entry.node) }}</option></select></label><template v-else><label>起点字段<select v-model="choices[item.object_key].source" :aria-label="`${item.name}起点字段`" :disabled="busy"><option value="" disabled>选择字段</option><option v-for="entry in options(item)" :key="entry.node.id" :value="entry.node.id">{{ datalinkNodeName(entry.node) }}</option></select></label><label>终点字段<select v-model="choices[item.object_key].target" :aria-label="`${item.name}终点字段`" :disabled="busy"><option value="" disabled>选择字段</option><option v-for="entry in options(item)" :key="entry.node.id" :value="entry.node.id">{{ datalinkNodeName(entry.node) }}</option></select></label></template></template></div></template>
      <p v-else-if="!isCandidate && !viewingDiff" class="history-notice">将以此历史配置生成新的发布版本，后续分析使用新版本，已启动的运行保持原版本。</p>
      <template v-if="!viewingDiff">
        <label class="history-confirm"><input v-model="confirmed" type="checkbox" :disabled="busy || detailLoading" aria-label="确认发布版本变更" />{{ isCandidate ? '确认以上冲突处理，并发布新的有效版本' : '确认恢复此配置并发布为新的有效版本' }}</label>
        <div class="history-actions"><Button :disabled="!canSubmit" @click="submit">{{ busy ? '正在发布' : isCandidate ? '应用处理并发布' : '恢复并发布' }}</Button></div>
      </template>
    </DialogContent></Dialog>
  </section>
</template>

<style scoped>
.version-history { font-size: 12px; min-width: 0; }.history-toolbar, .history-pagination, .history-heading, .history-actions, .version-actions { display: flex; align-items: center; gap: 10px; }.history-toolbar, .history-heading { justify-content: space-between; }.history-pagination, .history-actions { justify-content: flex-end; padding: 12px 0; }.version-row { display: flex; align-items: center; gap: 14px; padding: 16px 0; border-bottom: 1px solid var(--workspace-border); }.version-row > div:first-child { min-width: 0; flex: 1; }.version-actions, .history-heading > button { flex-shrink: 0; }.version-actions { flex-wrap: wrap; justify-content: flex-end; }.version-row small { display: block; margin-top: 5px; color: var(--workspace-text-muted); overflow-wrap: anywhere; }.version-row p { margin: 6px 0; }.history-notice { color: var(--workspace-text-muted); font-size: 12px; overflow-wrap: anywhere; }.history-error { color: var(--workspace-state-error); overflow-wrap: anywhere; }.version-identity { overflow-wrap: anywhere; }.conflict-row { display: grid; gap: 10px; padding: 12px 0; border-bottom: 1px solid var(--workspace-border); font-size: 12px; }.conflict-row strong { overflow-wrap: anywhere; }.conflict-row p { margin: 0; }.conflict-row label { display: grid; gap: 6px; }.conflict-row select { width: 100%; min-width: 0; height: 36px; border: 1px solid var(--workspace-border); background: var(--workspace-surface-inset); color: var(--workspace-text); border-radius: 6px; padding: 6px; }.history-confirm { display: flex; align-items: flex-start; gap: 8px; font-size: 12px; line-height: 1.6; }.history-confirm input { margin-top: 4px; accent-color: var(--primary); }@media(max-width: 560px) { .version-row { flex-wrap: wrap; }.version-row > div:first-child { flex-basis: 100%; }.version-actions { width: 100%; } }
</style>
