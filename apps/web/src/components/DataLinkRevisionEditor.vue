<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { FilePenLine, Plus, Save, Upload, Undo2, X, RotateCcw, Link2 } from "@lucide/vue";
import { getDatalinkCatalog, getDatalinkCatalogDetail, getDatalinkDraft, getDatalinkRelations, previewDatalinkDraft, publishDatalinkDraft, saveDatalinkDraft } from "@/api/datasources";
import { ApiClientError } from "@/api/client";
import type { DataLinkCatalogItem, DataLinkCatalogRelation, DataLinkDraft, DataLinkDraftChange, DataLinkDraftPreview } from "@/api/types";
import DataLinkSemanticResult from "@/components/DataLinkSemanticResult.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { datalinkMappedSummary, datalinkNodeName } from "@/lib/datalinkDisplay";

const props = defineProps<{ datasourceId: string; graphVersion: string; schemaRevision: number }>();
const emit = defineEmits<{ published: [] }>();
const open = ref(false);
const mode = ref<"review" | "node" | "relation" | "new-node" | "mapping">("review");
const draft = ref<DataLinkDraft | null>(null);
const serverRevision = ref<number | null>(null);
const objectLabels = ref<Record<string, string>>({});
const changes = ref<DataLinkDraftChange[]>([]);
const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const ready = ref(false);
const columns = ref<DataLinkCatalogItem[]>([]);
const node = ref<DataLinkCatalogItem | null>(null);
const relation = ref<DataLinkCatalogRelation | null>(null);
const description = ref("");
const aliases = ref("");
const semanticType = ref("");
const sourceId = ref("");
const targetId = ref("");
const enabled = ref(false);
const relationAction = ref<"status" | "repoint">("status");
const nodeType = ref<"concept" | "entity">("concept");
const newNodeName = ref("");
const newNodeDescription = ref("");
const newNodeAliases = ref("");
const targetIds = ref<string[]>([]);
const concepts = ref<DataLinkCatalogItem[]>([]);
const mappingLoading = ref(false);
const formBaseline = ref("");
const formValue = computed(() => JSON.stringify(mode.value === "node" ? [description.value, aliases.value, semanticType.value] : mode.value === "new-node" ? [nodeType.value, newNodeName.value, newNodeDescription.value, newNodeAliases.value] : mode.value === "mapping" ? targetIds.value : [sourceId.value, targetId.value, enabled.value, relationAction.value]));
const formDirty = computed(() => mode.value !== "review" && formBaseline.value !== formValue.value);
const mappingChoices = computed<DataLinkCatalogItem[]>(() => {
  const seen = new Set<string>();
  const staged = changes.value.flatMap((change) => {
    if (change.change_type !== "add_node" || change.node_type !== "concept") return [];
    seen.add(change.object_key);
    return [{ node: { id: change.object_key, name: change.name ?? change.object_key, type: "concept" as const, table: null, description: change.description ?? null, aliases: change.aliases ?? [], semantic_type: null, profile: null }, provenance: "manual" as const, mapping_count: 0, relation_count: 0, manual_created: true }];
  });
  return [...staged, ...concepts.value.filter((item) => !seen.has(item.node.id))];
});
const previewQuery = ref("");
const preview = ref<DataLinkDraftPreview | null>(null);
const previewBusy = ref(false);
let previewController: AbortController | null = null;
let previewGeneration = 0;
let generation = 0;
let controller: AbortController | null = null;
let publishKey: string | null = null;
const dirty = computed(() => JSON.stringify(changes.value) !== JSON.stringify(draft.value?.changes ?? []));
const hasUnsavedChanges = computed(() => dirty.value || formDirty.value || busy.value);
const stale = computed(() => !!draft.value && (draft.value.base_graph_version !== props.graphVersion || draft.value.schema_revision !== props.schemaRevision));
const editable = computed(() => ready.value && !loading.value && !busy.value && !stale.value);
const labels: Record<DataLinkDraftChange["change_type"], string> = { update_node: "修改业务语义", disable_relation: "禁用关系", enable_relation: "恢复关系", add_relation: "新增人工候选", repoint_relation: "修正关系端点", add_node: "新增语义对象", replace_mapping: "重配业务映射", reset_node: "恢复自动语义", reset_mapping: "恢复自动映射", reset_relation: "恢复自动关系" };

function message(caught: unknown): string {
  return caught instanceof ApiClientError ? `${caught.message}（${caught.code}，请求 ${caught.requestId}）` : "无法完成草稿操作，请重试。";
}

function changeFamily(type: DataLinkDraftChange["change_type"]): "node" | "mapping" | "relation" {
  if (type === "update_node" || type === "reset_node" || type === "add_node") return "node";
  if (type === "replace_mapping" || type === "reset_mapping") return "mapping";
  return "relation";
}

function replaceChange(change: DataLinkDraftChange): void {
  changes.value = [...changes.value.filter((item) => !(item.object_key === change.object_key && changeFamily(item.change_type) === changeFamily(change.change_type))), change];
}

function orderedChanges(): DataLinkDraftChange[] {
  return [...changes.value.filter((item) => item.change_type === "add_node"), ...changes.value.filter((item) => item.change_type !== "add_node")];
}

function automaticText(item: DataLinkCatalogItem | null): string {
  const value = item?.automatic;
  if (!value) return "";
  return [value.description || "说明留空", value.aliases.length ? value.aliases.join("、") : "无别名", value.semantic_type || "无语义类型"].join(" · ");
}

async function load(): Promise<void> {
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  const ticket = ++generation;
  loading.value = true;
  ready.value = false;
  error.value = "";
  try {
    const result = await getDatalinkDraft(props.datasourceId, signal);
    if (ticket !== generation) return;
    if (result && result.datasource_id !== props.datasourceId) throw new Error("Draft identity mismatch");
    serverRevision.value = result?.draft_revision ?? null;
    draft.value = result?.status === "active" ? result : null;
    changes.value = (draft.value?.changes ?? []).map((change) => ({ ...change, ...(change.aliases ? { aliases: [...change.aliases] } : {}) }));
    publishKey = null;
    ready.value = true;
  } catch (caught) {
    if (ticket === generation && !signal.aborted) error.value = message(caught);
  } finally {
    if (ticket === generation) loading.value = false;
  }
}

async function loadColumns(): Promise<void> {
  if (columns.value.length) return;
  const ticket = generation;
  const signal = controller?.signal;
  const items: DataLinkCatalogItem[] = [];
  let page = 1;
  try {
    while (true) {
      const result = await getDatalinkCatalog(props.datasourceId, { graphVersion: props.graphVersion, type: "column", page, pageSize: 100 }, signal);
      if (ticket !== generation) return;
      if (result.datasource_id !== props.datasourceId || result.graph_version !== props.graphVersion) throw new Error("Catalog identity mismatch");
      items.push(...result.items);
      if (items.length >= result.total || !result.items.length) break;
      page += 1;
    }
    columns.value = items;
    objectLabels.value = { ...objectLabels.value, ...Object.fromEntries(items.map((item) => [item.node.id, datalinkNodeName(item.node)])) };
    if (changes.value.some((change) => change.change_type !== "update_node" && change.change_type !== "add_relation")) {
      let relationPage = 1;
      while (true) {
        const result = await getDatalinkRelations(props.datasourceId, { graphVersion: props.graphVersion, page: relationPage, pageSize: 100 }, signal);
        if (ticket !== generation) return;
        for (const item of result.items) objectLabels.value[item.id] = `${datalinkNodeName(item.source)} → ${datalinkNodeName(item.target)}`;
        if (relationPage * 100 >= result.total || !result.items.length) break;
        relationPage += 1;
      }
    }
  } catch (caught) {
    if (ticket === generation && !signal?.aborted) error.value = message(caught);
  }
}

function editNode(item: DataLinkCatalogItem): void {
  node.value = item;
  objectLabels.value[item.node.id] = datalinkNodeName(item.node);
  const existing = changes.value.find((change) => change.object_key === item.node.id && change.change_type === "update_node");
  description.value = existing?.description ?? item.node.description ?? "";
  aliases.value = (existing?.aliases ?? item.node.aliases).join("\n");
  semanticType.value = existing?.semantic_type ?? item.node.semantic_type ?? "";
  mode.value = "node";
  formBaseline.value = formValue.value;
  open.value = true;
}

function newSemanticNode(type: "concept" | "entity"): void {
  mode.value = "new-node";
  nodeType.value = type;
  newNodeName.value = "";
  newNodeDescription.value = "";
  newNodeAliases.value = "";
  formBaseline.value = formValue.value;
  open.value = true;
}

async function editMapping(item: DataLinkCatalogItem): Promise<void> {
  if (item.node.type !== "column" && item.node.type !== "entity") return;
  node.value = item;
  mode.value = "mapping";
  targetIds.value = [];
  mappingLoading.value = true;
  open.value = true;
  const ticket = generation;
  const signal = controller?.signal;
  try {
    const items: DataLinkCatalogItem[] = [];
    let page = 1;
    const [detailResult] = await Promise.all([
      getDatalinkCatalogDetail(props.datasourceId, item.node.id, { graphVersion: props.graphVersion, page: 1, pageSize: 100 }, signal),
      (async () => {
        while (true) {
          const result = await getDatalinkCatalog(props.datasourceId, { graphVersion: props.graphVersion, type: "concept", page, pageSize: 100 }, signal);
          if (ticket !== generation) return;
          if (result.datasource_id !== props.datasourceId || result.graph_version !== props.graphVersion) throw new Error("Mapping identity mismatch");
          items.push(...result.items);
          if (items.length >= result.total || !result.items.length) break;
          page += 1;
        }
      })(),
    ]);
    if (ticket !== generation) return;
    if (detailResult.datasource_id !== props.datasourceId || detailResult.graph_version !== props.graphVersion) throw new Error("Mapping identity mismatch");
    const existing = changes.value.find((change) => change.object_key === item.node.id && (change.change_type === "replace_mapping" || change.change_type === "reset_mapping"));
    targetIds.value = existing?.change_type === "replace_mapping" ? [...(existing.target_ids ?? [])] : detailResult.mappings.map((mapping) => mapping.concept.id);
    concepts.value = items;
    formBaseline.value = formValue.value;
  } catch (caught) {
    if (ticket === generation && !signal?.aborted) error.value = message(caught);
  } finally { if (ticket === generation) mappingLoading.value = false; }
}

function editRelation(item: DataLinkCatalogRelation | null): void {
  relation.value = item;
  if (item) objectLabels.value[item.id] = `${datalinkNodeName(item.source)} → ${datalinkNodeName(item.target)}`;
  const existing = item ? changes.value.find((change) => change.object_key === item.id) : undefined;
  sourceId.value = existing?.source_id ?? item?.source.id ?? "";
  targetId.value = existing?.target_id ?? item?.target.id ?? "";
  relationAction.value = existing?.change_type === "repoint_relation" ? "repoint" : "status";
  enabled.value = existing ? existing.change_type === "enable_relation" || existing.enabled === true : item?.enabled ?? false;
  mode.value = "relation";
  formBaseline.value = formValue.value;
  open.value = true;
  void loadColumns();
}

function stage(): void {
  if (!editable.value) return;
  let change: DataLinkDraftChange;
  if (mode.value === "new-node") {
    const values = newNodeAliases.value.split("\n").map((value) => value.trim()).filter(Boolean);
    if (!newNodeName.value.trim()) { error.value = "请输入属性或实体名称。"; return; }
    change = { change_type: "add_node", object_key: `manual_${crypto.randomUUID()}`, node_type: nodeType.value, name: newNodeName.value.trim(), description: newNodeDescription.value, aliases: [...new Set(values)] };
  } else if (mode.value === "mapping" && node.value) {
    change = { change_type: "replace_mapping", object_key: node.value.node.id, target_ids: [...targetIds.value] };
  } else if (mode.value === "node" && node.value) {
    const values = aliases.value.split("\n").map((value) => value.trim()).filter(Boolean);
    if (values.length > 20 || values.some((value) => value.length > 200)) { error.value = "别名最多 20 个，每个最多 200 字。"; return; }
    change = { change_type: "update_node", object_key: node.value.node.id, description: description.value, aliases: [...new Set(values)], semantic_type: semanticType.value };
  } else {
    if ((!relation.value || relationAction.value === "repoint") && (!sourceId.value || !targetId.value || sourceId.value === targetId.value)) { error.value = "请选择两个不同的物理字段。"; return; }
    change = relation.value && relationAction.value === "status"
      ? { change_type: enabled.value ? "enable_relation" : "disable_relation", object_key: relation.value.id }
      : { change_type: relation.value ? "repoint_relation" : "add_relation", object_key: relation.value?.id ?? `manual_${crypto.randomUUID()}`, source_id: sourceId.value, target_id: targetId.value, relation_type: "joinable", enabled: enabled.value };
  }
  replaceChange(change);
  error.value = "";
  notice.value = "修改尚未保存";
  mode.value = "review";
}

async function save(): Promise<void> {
  if (!editable.value || !dirty.value) return;
  const ticket = generation;
  busy.value = true;
  error.value = "";
  try {
    const result = await saveDatalinkDraft(props.datasourceId, { base_graph_version: props.graphVersion, schema_revision: props.schemaRevision, expected_draft_revision: serverRevision.value, changes: orderedChanges() }, controller?.signal);
    if (ticket !== generation) return;
    draft.value = result;
    serverRevision.value = result.draft_revision;
    changes.value = result.changes.map((change) => ({ ...change, ...(change.aliases ? { aliases: [...change.aliases] } : {}) }));
    publishKey = null;
    notice.value = "草稿已保存，当前发布版未改变";
  } catch (caught) {
    if (ticket === generation) error.value = message(caught);
  } finally { if (ticket === generation) busy.value = false; }
}

async function publish(): Promise<void> {
  if (!editable.value || dirty.value || !draft.value || !changes.value.length) return;
  const ticket = generation;
  busy.value = true;
  error.value = "";
  publishKey ??= crypto.randomUUID();
  try {
    await publishDatalinkDraft(props.datasourceId, { expected_head: props.graphVersion, expected_draft_revision: draft.value.draft_revision, idempotency_key: publishKey }, controller?.signal);
    if (ticket !== generation) return;
    ready.value = false;
    notice.value = "发布完成，正在刷新当前版本";
    open.value = false;
    emit("published");
  } catch (caught) {
    if (ticket === generation) error.value = message(caught);
  } finally { if (ticket === generation) busy.value = false; }
}

function fieldName(id: string | null | undefined): string {
  if (id && objectLabels.value[id]) return objectLabels.value[id];
  const item = columns.value.find((entry) => entry.node.id === id);
  return item ? datalinkNodeName(item.node) : id ?? "未指定";
}

function clearPreview(): void {
  previewGeneration += 1; previewController?.abort(); preview.value = null; previewBusy.value = false;
}

function leaveForm(close: boolean): void {
  if (busy.value || (formDirty.value && !window.confirm("表单修改尚未加入草稿，确定放弃吗？"))) return;
  mode.value = "review";
  if (close) open.value = false;
}

function resetNode(): void {
  if (!node.value) return;
  replaceChange({ change_type: "reset_node", object_key: node.value.node.id });
  notice.value = "已加入恢复自动值操作，保存草稿后生效";
  mode.value = "review";
}

function resetMapping(): void {
  if (!node.value) return;
  replaceChange({ change_type: "reset_mapping", object_key: node.value.node.id });
  notice.value = "已加入恢复自动映射操作，保存草稿后生效";
  mode.value = "review";
}

function resetRelation(): void {
  if (!relation.value) return;
  replaceChange({ change_type: "reset_relation", object_key: relation.value.id });
  notice.value = "已加入恢复自动关系操作，保存草稿后生效";
  mode.value = "review";
}

function beforeUnload(event: BeforeUnloadEvent): void {
  if (hasUnsavedChanges.value) { event.preventDefault(); event.returnValue = ""; }
}
onMounted(() => window.addEventListener("beforeunload", beforeUnload));

async function runPreview(): Promise<void> {
  if (!editable.value || dirty.value || !draft.value || !previewQuery.value.trim()) return;
  clearPreview();
  previewController = new AbortController();
  const ticket = previewGeneration;
  previewBusy.value = true;
  error.value = "";
  try {
    const result = await previewDatalinkDraft(props.datasourceId, { expected_draft_revision: draft.value.draft_revision, query: previewQuery.value.trim(), max_nodes: 12 }, previewController.signal);
    if (ticket !== previewGeneration) return;
    if (result.datasource_id !== props.datasourceId || result.base_graph_version !== props.graphVersion || result.schema_revision !== props.schemaRevision || result.draft_revision !== draft.value?.draft_revision) throw new Error("Preview identity mismatch");
    preview.value = result;
  } catch (caught) { if (ticket === previewGeneration) error.value = message(caught); }
  finally { if (ticket === previewGeneration) previewBusy.value = false; }
}

watch([previewQuery, changes, () => props.datasourceId, () => props.graphVersion, () => props.schemaRevision], clearPreview, { deep: true, flush: "sync" });

watch(relationAction, (value, previous) => { if (value === 'repoint' && previous === 'status') enabled.value = false; }, { flush: "sync" });
watch([() => props.datasourceId, () => props.graphVersion, () => props.schemaRevision], () => {
  open.value = false; mode.value = "review"; node.value = null; relation.value = null; columns.value = []; objectLabels.value = {}; draft.value = null; serverRevision.value = null; changes.value = []; busy.value = false; notice.value = "";
  void load();
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { generation += 1; controller?.abort(); clearPreview(); window.removeEventListener("beforeunload", beforeUnload); });
defineExpose({ editNode, editRelation, editMapping, newSemanticNode, hasUnsavedChanges });
</script>

<template>
  <div class="revision-toolbar">
    <span role="status">{{ loading ? '正在读取草稿' : dirty ? `${changes.length} 项待保存` : draft ? `草稿 r${draft.draft_revision} · ${changes.length} 项修改` : '当前发布版' }}</span>
    <Button size="sm" variant="outline" @click="mode = 'review'; open = true; loadColumns()"><FilePenLine :size="14" />草稿与发布</Button>
  </div>
  <Dialog :open="open" @update:open="(value) => { if (!value) leaveForm(true); }">
    <DialogContent class="revision-dialog sm:max-w-[760px]">
      <DialogHeader><div class="revision-heading"><DialogTitle>{{ mode === 'node' ? '编辑业务语义' : mode === 'new-node' ? (nodeType === 'concept' ? '新增业务属性' : '新增业务实体') : mode === 'mapping' ? '重配业务映射' : mode === 'relation' ? (relation ? '修订关系' : '新增人工候选关系') : '草稿与发布' }}</DialogTitle><Button variant="ghost" size="icon" :disabled="busy" aria-label="关闭编辑器" title="关闭编辑器" @click="leaveForm(true)"><X :size="16" /></Button></div><DialogDescription class="revision-version">基于 {{ graphVersion }} · Schema {{ schemaRevision }}</DialogDescription></DialogHeader>
      <p v-if="error" role="alert" class="revision-error">{{ error }}</p>
      <p v-if="stale" role="alert" class="revision-error">草稿基线已过期，无法保存或发布。需要对齐当前版本后再处理修改。</p>
      <p v-if="notice" role="status" class="revision-notice">{{ notice }}</p>
      <Button v-if="(!ready || error) && !loading" variant="outline" :disabled="busy" @click="load">{{ dirty ? '丢弃本地输入并重新读取草稿' : '重新读取草稿' }}</Button>
      <p v-if="loading" role="status">正在读取草稿…</p>
      <form v-else-if="mode === 'new-node'" class="revision-form" @submit.prevent="stage">
        <strong>{{ nodeType === 'concept' ? '新增业务属性' : '新增业务实体' }}</strong>
        <label>名称<Input v-model="newNodeName" aria-label="新语义名称" :maxlength="200" /></label>
        <label>业务说明<Textarea v-model="newNodeDescription" aria-label="新语义说明" :maxlength="1000" rows="4" /></label>
        <label>别名（每行一个）<Textarea v-model="newNodeAliases" aria-label="新语义别名" rows="3" /></label>
        <div class="revision-actions"><Button variant="ghost" type="button" @click="leaveForm(false)">返回草稿</Button><Button type="submit" :disabled="!editable">加入草稿</Button></div>
      </form>
      <form v-else-if="mode === 'mapping'" class="revision-form" @submit.prevent="stage">
        <strong>{{ node ? datalinkNodeName(node.node) : '' }} · 重配业务属性</strong>
        <p class="revision-notice">选择一个或多个属性。清空选择会写入无映射状态；恢复自动映射请使用单独操作。</p>
        <p v-if="mappingLoading" role="status">正在读取当前映射…</p>
        <label v-for="item in mappingChoices" v-else :key="item.node.id" class="revision-checkbox"><input v-model="targetIds" type="checkbox" :value="item.node.id" />{{ datalinkNodeName(item.node) }}<small v-if="item.manual_created">未保存新增</small></label>
        <div class="revision-actions"><Button v-if="node?.can_reset_mapping" variant="outline" type="button" @click="resetMapping"><RotateCcw :size="14" />恢复自动映射</Button><Button variant="ghost" type="button" @click="leaveForm(false)">返回草稿</Button><Button type="submit" :disabled="!editable || mappingLoading">加入草稿</Button></div>
      </form>
      <form v-else-if="mode === 'node'" class="revision-form" @submit.prevent="stage">
        <strong>{{ node ? datalinkNodeName(node.node) : '' }}</strong>
        <p v-if="node && ['concept', 'entity'].includes(node.node.type)" class="revision-notice">共享语义对象，关联 {{ node.mapping_count }} 条字段映射。</p>
        <p v-if="node?.automatic" class="revision-notice">自动值：{{ automaticText(node) }}</p>
        <p v-if="node?.node.type === 'column' && !description.trim() && node.primary_mapping" class="revision-notice">列表上的映射说明不会写入字段，除非你在此保存。当前映射：{{ datalinkMappedSummary(node) }}</p>
        <label>业务说明<Textarea v-model="description" aria-label="业务说明" :maxlength="1000" rows="4" /></label>
        <label>别名（每行一个）<Textarea v-model="aliases" aria-label="别名" rows="3" /></label>
        <label>语义类型<Input v-model="semanticType" aria-label="语义类型" :maxlength="120" /></label>
        <div class="revision-actions"><Button v-if="node?.can_reset_node" variant="outline" type="button" @click="resetNode"><RotateCcw :size="14" />恢复自动值</Button><Button v-if="node && (node.node.type === 'column' || node.node.type === 'entity')" variant="outline" type="button" @click="editMapping(node)"><Link2 :size="14" />编辑映射</Button><Button variant="ghost" type="button" @click="leaveForm(false)">返回草稿</Button><Button type="submit" :disabled="!editable">加入草稿</Button></div>
      </form>
      <form v-else-if="mode === 'relation'" class="revision-form" @submit.prevent="stage">
        <p v-if="relation" class="revision-notice">{{ datalinkNodeName(relation.source) }} → {{ datalinkNodeName(relation.target) }}</p>
        <label v-if="relation">修订方式<select v-model="relationAction" aria-label="修订方式"><option value="status">禁用 / 恢复</option><option value="repoint">修正端点</option></select></label>
        <template v-if="!relation || relationAction === 'repoint'">
          <label>起点字段<select v-model="sourceId" aria-label="起点字段" required><option value="" disabled>选择字段</option><option v-for="item in columns" :key="item.node.id" :value="item.node.id">{{ datalinkNodeName(item.node) }}</option></select></label>
          <label>终点字段<select v-model="targetId" aria-label="终点字段" required><option value="" disabled>选择字段</option><option v-for="item in columns" :key="item.node.id" :value="item.node.id">{{ datalinkNodeName(item.node) }}</option></select></label>
          <p class="revision-notice">人工候选 · 单列等值关联 · 尚未数据核验 · 无统计置信度</p>
        </template>
        <label class="revision-checkbox"><input v-model="enabled" type="checkbox" aria-label="启用关系" />{{ relationAction === 'status' && relation ? '启用关系' : '明确启用此人工候选' }}</label>
        <div class="revision-actions"><Button v-if="relation?.provenance === 'manual'" variant="outline" type="button" @click="resetRelation"><RotateCcw :size="14" />恢复自动关系</Button><Button variant="ghost" type="button" @click="leaveForm(false)">返回草稿</Button><Button type="submit" :disabled="!editable">加入草稿</Button></div>
      </form>
      <template v-else>
        <div class="revision-compose">
          <Button size="sm" variant="outline" :disabled="!editable" @click="newSemanticNode('concept')"><Plus :size="14" />新增属性</Button>
          <Button size="sm" variant="outline" :disabled="!editable" @click="newSemanticNode('entity')"><Plus :size="14" />新增实体</Button>
          <Button size="sm" variant="outline" :disabled="!editable" @click="editRelation(null)"><Plus :size="14" />新增候选关系</Button>
        </div>
        <p v-if="!changes.length" class="revision-notice">暂无草稿修改</p>
        <div v-for="(change, index) in changes" :key="`${change.change_type}:${change.object_key}`" class="revision-change">
          <div><strong>{{ labels[change.change_type] }}</strong><small>{{ fieldName(change.object_key) }}</small><p v-if="change.change_type === 'update_node'">{{ change.description || '说明留空' }}<br />别名：{{ change.aliases?.join('、') || '无' }} · 语义类型：{{ change.semantic_type || '无' }}</p><p v-if="change.source_id">{{ fieldName(change.source_id) }} → {{ fieldName(change.target_id) }}<br />{{ change.enabled ? '启用 · 未核验' : '未启用 · 未核验' }}</p></div>
          <Button variant="ghost" size="icon" :disabled="!editable" aria-label="撤销此项修改" title="撤销此项修改" @click="changes.splice(index, 1)"><Undo2 :size="15" /></Button>
        </div>
        <div class="revision-actions"><Button variant="outline" :disabled="!editable || !dirty" @click="save"><Save :size="14" />{{ busy ? '正在提交' : '保存草稿' }}</Button><Button :disabled="!editable || dirty || !changes.length || !draft" @click="publish"><Upload :size="14" />发布修改</Button></div>
        <form class="revision-form" @submit.prevent="runPreview"><label>草稿检索问题<Input v-model="previewQuery" aria-label="草稿检索问题" :maxlength="2000" /></label><Button variant="outline" :disabled="!editable || dirty || !draft || !previewQuery.trim() || previewBusy">{{ previewBusy ? '正在检索' : '检索已保存草稿' }}</Button></form>
        <section v-if="preview"><p class="revision-notice">草稿 r{{ preview.draft_revision }} · 关键词检索{{ preview.is_truncated ? ' · 已达到返回上限' : '' }}</p><DataLinkSemanticResult :context="preview.semantic_context" /></section>
      </template>
    </DialogContent>
  </Dialog>
</template>

<style scoped>
.revision-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.revision-heading > :first-child { min-width: 0; overflow-wrap: anywhere; }.revision-heading > button { flex-shrink: 0; }.revision-version { overflow-wrap: anywhere; min-width: 0; }
.revision-toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }.revision-toolbar > span { margin-right: auto; color: var(--workspace-text-muted); font-size: 11px; }
@media (max-width: 680px) { .revision-toolbar { margin-bottom: 10px; }.revision-toolbar :deep(button), .revision-actions :deep(button) { min-height: 44px; } }.revision-compose { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
.revision-form { display: grid; gap: 16px; min-width: 0; }.revision-form label { display: grid; gap: 7px; font-size: 12px; }.revision-form select { width: 100%; min-width: 0; height: 36px; border-radius: 6px; border: 1px solid var(--workspace-border); background: var(--workspace-surface-inset); color: var(--workspace-text); padding: 6px; }.revision-form .revision-checkbox { display: flex; align-items: center; gap: 9px; }.revision-checkbox input { accent-color: var(--primary); }.revision-checkbox small { color: var(--workspace-text-muted); }
.revision-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 10px; padding-top: 12px; }.revision-notice { color: var(--workspace-text-muted); font-size: 12px; overflow-wrap: anywhere; }.revision-error { color: var(--workspace-state-error); font-size: 12px; overflow-wrap: anywhere; }.revision-change { display: flex; gap: 12px; justify-content: space-between; padding: 14px 0; border-bottom: 1px solid var(--workspace-border); font-size: 12px; }.revision-change > div { min-width: 0; overflow-wrap: anywhere; }.revision-change small { display: block; color: var(--workspace-text-muted); margin-top: 5px; }.revision-change p { line-height: 1.7; }.revision-change button { flex-shrink: 0; }
</style>
