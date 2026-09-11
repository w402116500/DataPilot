<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ArrowLeft, ArrowRight, ArrowUpRight, BookOpen, GitBranch, History, Layers, Network, Play, RefreshCw, Search, X } from "@lucide/vue";
import { cancelDatalinkValidation, createDatalinkValidation, getDatalinkCatalog, getDatalinkCatalogDetail, getDatalinkRelations, listDatalinkValidations, previewDatalink } from "@/api/datasources";
import { ApiClientError } from "@/api/client";
import type { DataLinkCatalog, DataLinkCatalogDetail, DataLinkCatalogItem, DataLinkNodeType, DataLinkPreview, DataLinkPreviewRequest, DataLinkRelations, DataLinkValidation } from "@/api/types";
import DataLinkValidationDialog from "@/components/DataLinkValidationDialog.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import DataLinkSemanticResult from "@/components/DataLinkSemanticResult.vue";
import DataLinkRevisionEditor from "@/components/DataLinkRevisionEditor.vue";
import DataLinkVersionHistory from "@/components/DataLinkVersionHistory.vue";
import { compactCatalogMappings, DATALINK_LIST_PAGE_SIZE, FIELD_CATALOG_FETCH_PAGE_SIZE, datalinkConfidence, datalinkMappedColumnList, datalinkMappedSummary, datalinkNodeLabels, datalinkNodeName, datalinkProvenanceLabels, datalinkRelationLabels, fieldHasManualNote, fieldRelationConcept, fieldRelationEntity, fieldRowConcept, fieldRowDescription, fieldRowEntity, groupCatalogByTable, groupRelationsByTable, indexCatalogColumns, paginateTableGroups, relationRowStatus, uniqueFieldRelationCount, type FieldRelationRow } from "@/lib/datalinkDisplay";

const props = defineProps<{ datasourceId: string; graphVersion: string; schemaRevision: number }>();
const emit = defineEmits<{ published: [] }>();
const revisionEditor = ref<InstanceType<typeof DataLinkRevisionEditor> | null>(null);
const hasUnsavedChanges = computed(() => revisionEditor.value?.hasUnsavedChanges ?? false);
defineExpose({ hasUnsavedChanges });
type WorkspaceTab = "fields" | "concepts" | "relations" | "graph" | "versions";
const tabs = [
  { value: "fields", label: "字段说明书", icon: BookOpen },
  { value: "concepts", label: "业务属性", icon: Layers },
  { value: "relations", label: "关系", icon: GitBranch },
  { value: "graph", label: "语义地图", icon: Network },
  { value: "versions", label: "版本", icon: History },
] as const;
const activeTab = ref<WorkspaceTab>("fields");
const conceptKind = ref<Extract<DataLinkNodeType, "concept" | "entity">>("concept");
const previewOpen = ref(false);
const search = ref("");
const page = ref(1);
const catalog = ref<DataLinkCatalog | null>(null);
const relations = ref<DataLinkRelations | null>(null);
const loading = ref(false);
const error = ref("");
const detail = ref<DataLinkCatalogDetail | null>(null);
const selectedItem = ref<DataLinkCatalogItem | null>(null);
const selectedRelationRow = ref<FieldRelationRow | null>(null);
const selectedRelation = computed(() => selectedRelationRow.value?.relation ?? null);
const detailPage = ref(1);
const detailLoading = ref(false);
const detailError = ref("");
const question = ref("");
const focus = ref<NonNullable<DataLinkPreviewRequest["focus"]> | "">("");
const maxNodes = ref(12);
const preview = ref<DataLinkPreview | null>(null);
const previewLoading = ref(false);
const previewError = ref("");
let listRequest = 0;
let detailRequest = 0;
let previewRequest = 0;
let listAbort: AbortController | null = null;
let detailAbort: AbortController | null = null;
let previewAbort: AbortController | null = null;
let searchTimer: ReturnType<typeof setTimeout> | null = null;
const validations = ref<DataLinkValidation[]>([]);
const validationError = ref("");
const validationLoading = ref(false);
const validating = ref(false);
const selectedValidation = ref<DataLinkValidation | null>(null);
let validationRequest = 0;
let validationAbort: AbortController | null = null;
const relationValidations = computed(() => selectedRelation.value ? validations.value.filter((item) => item.relation_id === selectedRelation.value?.id) : []);
const canValidateRelation = computed(() => selectedRelation.value !== null && ["foreign_key", "joinable"].includes(selectedRelation.value.type) && Boolean(selectedRelation.value.source.table && selectedRelation.value.target.table));
const validationStatusLabel: Record<DataLinkValidation["status"], string> = {
  running: "进行中", completed: "已完成", partial: "部分完成", failed: "失败", canceled: "已取消", interrupted: "已中断",
};
const columnIndex = computed(() => indexCatalogColumns(catalog.value?.items ?? []));
const relationTableGroups = computed(() => groupRelationsByTable(relations.value?.items ?? []));
const listTotal = computed(() => activeTab.value === "relations" ? uniqueFieldRelationCount(relationTableGroups.value) : catalog.value?.total ?? 0);
const fieldPages = computed(() => paginateTableGroups(groupCatalogByTable(catalog.value?.items ?? [])));
const relationPages = computed(() => paginateTableGroups(relationTableGroups.value));
const pageCount = computed(() => {
  if (activeTab.value === "fields") return Math.max(1, fieldPages.value.length);
  if (activeTab.value === "relations") return Math.max(1, relationPages.value.length);
  return Math.max(1, Math.ceil(listTotal.value / DATALINK_LIST_PAGE_SIZE));
});
const dialogOpen = computed(() => selectedItem.value !== null || selectedRelationRow.value !== null);
const catalogListTabs: WorkspaceTab[] = ["fields", "concepts"];
const clientPagedTabs: WorkspaceTab[] = ["fields", "relations"];
const compactMappings = computed(() => compactCatalogMappings(detail.value?.mappings ?? []));
const fieldGroups = computed(() => fieldPages.value[Math.min(page.value, fieldPages.value.length) - 1] ?? []);
const relationGroups = computed(() => relationPages.value[Math.min(page.value, relationPages.value.length) - 1] ?? []);
function catalogType(): DataLinkNodeType {
  return activeTab.value === "concepts" ? conceptKind.value : "column";
}

function errorMessage(caught: unknown): string {
  if (caught instanceof ApiClientError) return `${caught.message}（${caught.code}，请求 ${caught.requestId}）`;
  return "暂时无法读取 DataLink，请重试。";
}

async function loadValidations(): Promise<void> {
  validationAbort?.abort();
  const controller = new AbortController();
  validationAbort = controller;
  const requestId = ++validationRequest;
  validationLoading.value = true;
  validationError.value = "";
  try {
    const result = await listDatalinkValidations(props.datasourceId, controller.signal);
    if (requestId === validationRequest) validations.value = result;
  } catch (caught) {
    if (requestId === validationRequest && !controller.signal.aborted) validationError.value = errorMessage(caught);
  } finally {
    if (requestId === validationRequest) validationLoading.value = false;
  }
}

async function runValidation(): Promise<void> {
  if (!selectedRelation.value || !canValidateRelation.value || validating.value) return;
  validating.value = true;
  validationError.value = "";
  try {
    const result = await createDatalinkValidation(props.datasourceId, {
      relation_id: selectedRelation.value.id,
      graph_version: props.graphVersion,
      schema_revision: props.schemaRevision,
      idempotency_key: crypto.randomUUID(),
    });
    validations.value = [result, ...validations.value.filter((item) => item.id !== result.id)];
    selectedValidation.value = result;
  } catch (caught) {
    validationError.value = errorMessage(caught);
  } finally {
    validating.value = false;
  }
}

async function cancelValidation(item: DataLinkValidation): Promise<void> {
  try {
    const result = await cancelDatalinkValidation(props.datasourceId, item.id);
    validations.value = validations.value.map((current) => current.id === result.id ? result : current);
    if (selectedValidation.value?.id === result.id) selectedValidation.value = result;
  } catch (caught) {
    validationError.value = errorMessage(caught);
  }
}

function invalidateList(): void {
  listRequest += 1;
  listAbort?.abort();
  loading.value = false;
  catalog.value = null;
  relations.value = null;
  validationRequest += 1;
  validationAbort?.abort();
  validations.value = [];
}

async function loadList(): Promise<void> {
  if (activeTab.value !== "fields" && activeTab.value !== "concepts" && activeTab.value !== "relations") return;
  listAbort?.abort();
  const controller = new AbortController();
  listAbort = controller;
  const requestId = ++listRequest;
  loading.value = true;
  error.value = "";
  const clientPaged = clientPagedTabs.includes(activeTab.value);
  const fetchPageSize = clientPaged ? FIELD_CATALOG_FETCH_PAGE_SIZE : DATALINK_LIST_PAGE_SIZE;
  const options = { graphVersion: props.graphVersion, query: search.value, page: clientPaged ? 1 : page.value, pageSize: fetchPageSize };
  async function collectAllPages<TItem>(
    first: { items: TItem[]; total: number },
    loadPage: (page: number) => Promise<{ items: TItem[] }>,
  ): Promise<{ items: TItem[]; total: number; page: number; page_size: number }> {
    if (first.total <= first.items.length) return { ...first, page: 1, page_size: first.items.length };
    const items = [...first.items];
    const totalPages = Math.ceil(first.total / fetchPageSize);
    for (let nextPage = 2; nextPage <= totalPages; nextPage += 1) {
      if (controller.signal.aborted || requestId !== listRequest) break;
      items.push(...(await loadPage(nextPage)).items);
    }
    return { ...first, items, page: 1, page_size: items.length };
  }
  try {
    if (catalogListTabs.includes(activeTab.value)) {
      const result = await getDatalinkCatalog(props.datasourceId, { ...options, type: catalogType() }, controller.signal);
      if (requestId !== listRequest) return;
      if (activeTab.value === "fields") {
        const collected = await collectAllPages(result, (nextPage) => getDatalinkCatalog(props.datasourceId, { ...options, type: catalogType(), page: nextPage }, controller.signal));
        if (requestId !== listRequest) return;
        catalog.value = { ...result, ...collected };
      } else {
        catalog.value = result;
      }
    } else {
      const relationOptions = { graphVersion: props.graphVersion, query: search.value, page: 1, pageSize: fetchPageSize };
      const columnOptions = { graphVersion: props.graphVersion, type: "column" as const, page: 1, pageSize: fetchPageSize };
      const [relationFirst, catalogFirst] = await Promise.all([
        getDatalinkRelations(props.datasourceId, relationOptions, controller.signal),
        getDatalinkCatalog(props.datasourceId, columnOptions, controller.signal),
      ]);
      if (requestId !== listRequest) return;
      const [collectedRelations, collectedCatalog] = await Promise.all([
        collectAllPages(relationFirst, (nextPage) => getDatalinkRelations(props.datasourceId, { ...relationOptions, page: nextPage }, controller.signal)),
        collectAllPages(catalogFirst, (nextPage) => getDatalinkCatalog(props.datasourceId, { ...columnOptions, page: nextPage }, controller.signal)),
      ]);
      if (requestId !== listRequest) return;
      relations.value = { ...relationFirst, ...collectedRelations };
      catalog.value = { ...catalogFirst, ...collectedCatalog };
      void loadValidations();
    }
  } catch (caught) {
    if (requestId === listRequest && !controller.signal.aborted) error.value = errorMessage(caught);
  } finally {
    if (requestId === listRequest) loading.value = false;
  }
}

function closeDetail(): void {
  detailRequest += 1;
  detailAbort?.abort();
  selectedItem.value = null;
  selectedRelationRow.value = null;
  detail.value = null;
  detailLoading.value = false;
  detailError.value = "";
}

async function loadDetail(): Promise<void> {
  if (!selectedItem.value) return;
  detailAbort?.abort();
  const controller = new AbortController();
  detailAbort = controller;
  const requestId = ++detailRequest;
  detailLoading.value = true;
  detailError.value = "";
  try {
    const result = await getDatalinkCatalogDetail(props.datasourceId, selectedItem.value.node.id, {
      graphVersion: props.graphVersion, page: detailPage.value, pageSize: 20,
    }, controller.signal);
    if (requestId === detailRequest) detail.value = result;
  } catch (caught) {
    if (requestId === detailRequest && !controller.signal.aborted) detailError.value = errorMessage(caught);
  } finally {
    if (requestId === detailRequest) detailLoading.value = false;
  }
}

function openItem(item: DataLinkCatalogItem): void {
  closeDetail();
  selectedItem.value = item;
  detailPage.value = 1;
  void loadDetail();
}

function turnDetailPage(delta: number): void {
  detailPage.value += delta;
  void loadDetail();
}

function invalidatePreview(): void {
  previewRequest += 1;
  previewAbort?.abort();
  previewLoading.value = false;
  preview.value = null;
  previewError.value = "";
}

async function runPreview(): Promise<void> {
  if (!question.value.trim() || !Number.isInteger(maxNodes.value) || maxNodes.value < 1 || maxNodes.value > 50) return;
  invalidatePreview();
  const controller = new AbortController();
  previewAbort = controller;
  const requestId = ++previewRequest;
  previewLoading.value = true;
  try {
    const result = await previewDatalink(props.datasourceId, {
      graph_version: props.graphVersion, query: question.value.trim(), focus: focus.value || null, max_nodes: maxNodes.value,
    }, controller.signal);
    if (requestId === previewRequest) preview.value = result;
  } catch (caught) {
    if (requestId === previewRequest && !controller.signal.aborted) previewError.value = errorMessage(caught);
  } finally {
    if (requestId === previewRequest) previewLoading.value = false;
  }
}

function editSelected(): void {
  if (selectedItem.value) revisionEditor.value?.editNode(selectedItem.value);
  else if (selectedRelation.value) revisionEditor.value?.editRelation(selectedRelation.value);
  closeDetail();
}

watch([() => props.datasourceId, () => props.graphVersion], () => {
  invalidateList();
  invalidatePreview();
  closeDetail();
  page.value = 1;
  void loadList();
}, { immediate: true, flush: "sync" });
watch([activeTab, conceptKind], () => {
  if (searchTimer !== null) clearTimeout(searchTimer);
  invalidateList();
  page.value = 1;
  void loadList();
});
watch(search, () => {
  invalidateList();
  if (searchTimer !== null) clearTimeout(searchTimer);
  page.value = 1;
  searchTimer = setTimeout(() => { void loadList(); }, 220);
}, { flush: "sync" });
watch(pageCount, (count) => {
  if (page.value > count) page.value = count;
});
watch([question, focus, maxNodes], invalidatePreview, { flush: "sync" });

function turnPage(delta: number): void {
  page.value += delta;
  if (clientPagedTabs.includes(activeTab.value)) return;
  void loadList();
}

onBeforeUnmount(() => {
  if (searchTimer !== null) clearTimeout(searchTimer);
  invalidateList();
  invalidatePreview();
  closeDetail();
});
</script>

<template>
  <section class="datalink-workspace" aria-label="DataLink 语义工作区">
    <DataLinkRevisionEditor ref="revisionEditor" :datasource-id="datasourceId" :graph-version="graphVersion" :schema-revision="schemaRevision" @published="emit('published')" />
    <div class="workspace-chrome">
      <div class="workspace-tabs" role="tablist" aria-label="DataLink 视图">
        <button v-for="tab in tabs" :id="`datalink-tab-${tab.value}`" :key="tab.value" type="button" role="tab" :aria-selected="activeTab === tab.value" :aria-controls="`datalink-panel-${tab.value}`" @click="activeTab = tab.value"><component :is="tab.icon" :size="15" />{{ tab.label }}</button>
      </div>
      <Button variant="outline" size="sm" @click="previewOpen = true"><Play :size="14" />载荷预览</Button>
    </div>
    <div :id="`datalink-panel-${activeTab}`" role="tabpanel" :aria-labelledby="`datalink-tab-${activeTab}`">
      <template v-if="activeTab === 'fields' || activeTab === 'concepts' || activeTab === 'relations'">
        <div class="list-toolbar">
          <label v-if="activeTab === 'concepts'" class="kind-filter"><span class="sr-only">语义对象类型</span><select v-model="conceptKind" aria-label="语义对象类型"><option value="concept">业务属性</option><option value="entity">业务实体</option></select></label>
          <label class="catalog-search"><Search :size="15" /><Input v-model="search" :placeholder="activeTab === 'relations' ? '搜索关系两端或类型' : '搜索名称、说明或别名'" aria-label="搜索 DataLink" /></label>
          <Button variant="ghost" size="icon" title="刷新列表" aria-label="刷新列表" :disabled="loading" @click="loadList"><RefreshCw :size="15" :class="{ spinning: loading }" /></Button>
        </div>
        <div v-if="error" class="panel-error" role="alert">{{ error }}<Button variant="outline" size="sm" @click="loadList">重试</Button></div>
        <p v-else-if="loading" class="panel-state" role="status">正在读取{{ activeTab === 'relations' ? '关系' : activeTab === 'concepts' ? '业务属性' : '字段说明书' }}…</p>
        <template v-else>
          <div v-if="activeTab === 'fields' && catalog?.items.length" class="catalog-list">
            <section v-for="group in fieldGroups" :key="group.table" class="field-group">
              <h3>{{ group.table }}</h3>
              <div class="catalog-head"><span>字段</span><span>属性</span><span>说明</span><span>实体</span><span /></div>
              <div v-for="item in group.items" :key="item.node.id" class="catalog-row">
                <button type="button" class="catalog-row-main" @click="openItem(item)">
                  <span class="node-name"><strong>{{ item.node.name }}</strong></span>
                  <span class="node-concept">{{ fieldRowConcept(item) }}</span>
                  <span class="node-description">{{ fieldRowDescription(item) }}<small v-if="fieldHasManualNote(item)">手写说明</small></span>
                  <span class="node-entity">{{ fieldRowEntity(item) }}</span>
                  <ArrowUpRight :size="15" />
                </button>
                <Button size="sm" variant="outline" @click="revisionEditor?.editNode(item)">快速编辑</Button>
              </div>
            </section>
          </div>
          <div v-else-if="activeTab === 'concepts' && catalog?.items.length" class="concept-list">
            <button v-for="item in catalog.items" :key="item.node.id" type="button" class="concept-card" @click="openItem(item)">
              <span class="node-name"><strong>{{ item.node.name }}</strong><small>{{ datalinkNodeLabels[item.node.type] }}</small></span>
              <span class="node-description">{{ item.node.description || '业务说明未记录' }}</span>
              <span class="mapped-columns">{{ datalinkMappedColumnList(item) || '未映射字段' }}</span>
            </button>
          </div>
          <div v-else-if="activeTab === 'relations' && relationTableGroups.length" class="catalog-list">
            <section v-for="group in relationGroups" :key="group.table" class="field-group">
              <h3>{{ group.table }}</h3>
              <div class="catalog-head relation-head"><span>字段</span><span>属性</span><span>关系</span><span>对端字段</span><span>对端属性</span><span /></div>
              <div v-for="row in group.items" :key="row.key" class="catalog-row">
                <button type="button" class="catalog-row-main relation-row-main" @click="selectedRelationRow = row">
                  <span class="node-name node-ref"><strong>{{ row.local.name }}</strong></span>
                  <span class="node-concept">{{ fieldRelationConcept(columnIndex, row.local) }}</span>
                  <span class="relation-kind"><strong>{{ datalinkRelationLabels[row.relation.type] }}</strong><small v-if="relationRowStatus(row.relation)" :class="{ 'join-label': row.relation.enabled && row.relation.join_eligible }">{{ relationRowStatus(row.relation) }}</small></span>
                  <span class="node-other node-ref"><strong>{{ datalinkNodeName(row.other) }}</strong></span>
                  <span class="node-other-concept">{{ fieldRelationConcept(columnIndex, row.other) }}</span>
                  <ArrowUpRight :size="15" />
                </button>
              </div>
            </section>
          </div>
          <p v-else class="panel-state">{{ search.trim() ? '没有匹配项' : '当前版本没有此类记录' }}</p>
        </template>
        <div class="list-pagination"><span>{{ listTotal }} 项</span><div><Button variant="ghost" size="icon" :disabled="loading || page <= 1" aria-label="上一页" title="上一页" @click="turnPage(-1)"><ArrowLeft :size="15" /></Button><span>{{ page }} / {{ pageCount }}</span><Button variant="ghost" size="icon" :disabled="loading || page >= pageCount" aria-label="下一页" title="下一页" @click="turnPage(1)"><ArrowRight :size="15" /></Button></div></div>
      </template>
      <DataLinkVersionHistory v-else-if="activeTab === 'versions'" :datasource-id="datasourceId" :graph-version="graphVersion" :schema-revision="schemaRevision" :has-unsaved-changes="hasUnsavedChanges" @published="emit('published')" />
      <slot v-else name="graph" />
    </div>
    <Dialog :open="previewOpen" @update:open="(open) => { previewOpen = open; }">
      <DialogContent class="preview-dialog">
        <DialogHeader>
          <DialogTitle>载荷预览</DialogTitle>
          <DialogDescription>查看当前问题会检索到的字段、关系、Join 路径和警告。</DialogDescription>
        </DialogHeader>
        <div class="preview-layout">
          <form class="preview-form" @submit.prevent="runPreview">
            <label>检索问题<Textarea v-model="question" aria-label="检索问题" :maxlength="1000" rows="4" placeholder="例如：订单金额与客户如何关联？" /></label>
            <div class="preview-options"><label>检索侧重<select v-model="focus" aria-label="检索侧重"><option value="">综合</option><option value="schema">结构与语义</option><option value="join_paths">Join 路径</option><option value="data_profile">数据画像检索</option></select></label><label>节点上限<Input v-model.number="maxNodes" type="number" min="1" max="50" aria-label="节点上限" /></label></div>
            <Button type="submit" :disabled="previewLoading || !question.trim() || !Number.isInteger(maxNodes) || maxNodes < 1 || maxNodes > 50"><Play :size="14" />{{ previewLoading ? '正在检索' : '运行检索' }}</Button>
            <p v-if="previewError" class="panel-error" role="alert">{{ previewError }}</p>
          </form>
          <div class="preview-content" :aria-busy="previewLoading">
            <p v-if="previewLoading" class="panel-state" role="status">正在检索当前版本…</p>
            <template v-else-if="preview"><div class="preview-result-heading"><strong>核对清单</strong><span>{{ { keyword: '关键词检索', hybrid: '关键词与向量检索', unknown: '检索方式未记录' }[preview.retrieval_mode] }}</span></div><p v-if="preview.is_truncated" class="truncation">结果已达到返回上限</p><DataLinkSemanticResult :context="preview.semantic_context" /></template>
            <p v-else class="panel-state">尚无检索结果</p>
          </div>
        </div>
      </DialogContent>
    </Dialog>
    <Dialog :open="dialogOpen" @update:open="(open) => { if (!open) closeDetail(); }">
      <DialogContent class="datalink-detail">
        <DialogHeader><div class="detail-heading"><DialogTitle>{{ selectedItem ? datalinkNodeName(selectedItem.node) : selectedRelationRow ? datalinkNodeName(selectedRelationRow.local) : '' }}</DialogTitle><Button variant="ghost" size="icon" aria-label="关闭详情" title="关闭详情" @click="closeDetail"><X :size="16" /></Button></div><DialogDescription>{{ selectedItem ? datalinkNodeLabels[selectedItem.node.type] : selectedRelationRow ? datalinkRelationLabels[selectedRelationRow.relation.type] : '' }}</DialogDescription></DialogHeader>
        <template v-if="selectedItem">
          <div class="relation-actions">
            <Button size="sm" variant="outline" @click="editSelected">编辑业务语义</Button>
            <Button v-if="selectedItem.node.type === 'column' || selectedItem.node.type === 'entity'" size="sm" variant="outline" @click="revisionEditor?.editMapping(selectedItem); closeDetail()">重配业务映射</Button>
          </div>
          <dl class="detail-metadata"><div><dt>业务说明</dt><dd>{{ selectedItem.node.description || '未记录' }}</dd></div><div v-if="selectedItem.node.type === 'column'"><dt>映射属性</dt><dd>{{ selectedItem.primary_mapping ? datalinkMappedSummary(selectedItem) : '未映射' }}<small v-if="selectedItem.primary_mapping?.entity_name"> · {{ selectedItem.primary_mapping.entity_name }}</small></dd></div><div><dt>别名</dt><dd>{{ selectedItem.node.aliases.join('、') || '未记录' }}</dd></div><div><dt>节点来源</dt><dd>{{ datalinkProvenanceLabels[selectedItem.provenance] }}</dd></div><div><dt>语义类型</dt><dd>{{ selectedItem.node.semantic_type || '未记录' }}</dd></div><div v-if="selectedItem.automatic"><dt>自动值</dt><dd>{{ [selectedItem.automatic.description || '说明留空', selectedItem.automatic.aliases.length ? selectedItem.automatic.aliases.join('、') : '无别名', selectedItem.automatic.semantic_type || '无语义类型'].join(' · ') }}</dd></div></dl>
          <div v-if="detailError" class="panel-error" role="alert">{{ detailError }}<Button size="sm" variant="outline" @click="loadDetail">重试</Button></div>
          <p v-else-if="detailLoading" role="status">正在读取映射…</p>
          <section v-else-if="detail"><h4>业务映射</h4><p v-if="!compactMappings.length" class="muted">未记录语义映射</p><div v-for="mapping in compactMappings" :key="`${mapping.column.id}:${mapping.concept.id}`" class="mapping-row"><strong v-if="selectedItem.node.type !== 'column'">{{ datalinkNodeName(mapping.column) }}</strong><span><template v-if="selectedItem.node.type !== 'column'">→ </template>{{ mapping.concept.name }} <small>{{ datalinkConfidence(mapping.fieldConfidence) }}</small></span><span v-if="mapping.entity">→ {{ mapping.entity.name }}</span><span v-else>→ 未关联实体</span><small v-if="mapping.extraEntities.length" class="extra-entities">其他归属：{{ mapping.extraEntities.map((item) => item.name).join('、') }}</small></div><div v-if="detail.total > 20" class="list-pagination"><span>{{ detail.total }} 条映射</span><div><Button variant="ghost" size="icon" aria-label="上一页映射" :disabled="detailPage <= 1" @click="turnDetailPage(-1)"><ArrowLeft :size="14" /></Button><span>{{ detailPage }} / {{ Math.ceil(detail.total / 20) }}</span><Button variant="ghost" size="icon" aria-label="下一页映射" :disabled="detailPage * 20 >= detail.total" @click="turnDetailPage(1)"><ArrowRight :size="14" /></Button></div></div></section>
        </template>
        <template v-else-if="selectedRelationRow">
          <div class="relation-actions">
            <Button v-if="['foreign_key', 'joinable'].includes(selectedRelationRow.relation.type)" size="sm" variant="outline" @click="editSelected">修订关系</Button>
            <Button v-if="canValidateRelation" size="sm" :disabled="validating" @click="runValidation">{{ validating ? "正在核验" : "核验关系" }}</Button>
          </div>
          <div class="relation-endpoints">
            <article class="relation-endpoint">
              <span class="endpoint-label">字段</span>
              <strong class="node-ref">{{ datalinkNodeName(selectedRelationRow.local) }}</strong>
              <span class="endpoint-mapping">{{ fieldRelationConcept(columnIndex, selectedRelationRow.local) }}<small v-if="fieldRelationEntity(columnIndex, selectedRelationRow.local)"> · {{ fieldRelationEntity(columnIndex, selectedRelationRow.local) }}</small></span>
            </article>
            <article class="relation-endpoint">
              <span class="endpoint-label">对端字段</span>
              <strong class="node-ref">{{ datalinkNodeName(selectedRelationRow.other) }}</strong>
              <span class="endpoint-mapping">{{ fieldRelationConcept(columnIndex, selectedRelationRow.other) }}<small v-if="fieldRelationEntity(columnIndex, selectedRelationRow.other)"> · {{ fieldRelationEntity(columnIndex, selectedRelationRow.other) }}</small></span>
            </article>
          </div>
          <dl class="detail-metadata">
            <div><dt>来源</dt><dd>{{ datalinkProvenanceLabels[selectedRelationRow.relation.provenance] }}</dd></div>
            <div v-if="relationRowStatus(selectedRelationRow.relation)"><dt>状态</dt><dd :class="{ 'join-label': selectedRelationRow.relation.enabled && selectedRelationRow.relation.join_eligible }">{{ relationRowStatus(selectedRelationRow.relation) }}</dd></div>
            <div><dt>{{ selectedRelationRow.relation.type === 'foreign_key' ? '声明置信度' : '原始推断分数' }}</dt><dd>{{ datalinkConfidence(selectedRelationRow.relation.confidence) }}</dd></div>
            <div><dt>生成依据</dt><dd>{{ selectedRelationRow.relation.evidence?.summary || '未记录' }}</dd></div>
          </dl>
          <p v-if="validationError" class="panel-error" role="alert">{{ validationError }}</p>
          <section class="validation-list">
            <h4>数据核验</h4>
            <p v-if="validationLoading" role="status">正在读取核验记录…</p>
            <p v-else-if="!relationValidations.length" class="muted">尚未对该关系执行完整数据核验</p>
            <button v-for="item in relationValidations" :key="item.id" type="button" class="validation-row" @click="selectedValidation = item">
              <strong :class="`status-${item.status}`">{{ validationStatusLabel[item.status] }}{{ item.expired ? " · 已过期" : "" }}</strong>
              <small>{{ item.direction === "source_to_target" ? "源 → 目标" : "目标 → 源" }} · r{{ item.schema_revision }}</small>
              <small>未匹配 {{ item.source_unmatched_count ?? "—" }} · 重复键 {{ item.target_duplicate_count ?? "—" }}</small>
              <Button v-if="item.status === 'running'" size="sm" variant="outline" @click.stop="cancelValidation(item)">取消</Button>
            </button>
          </section>
        </template>
      </DialogContent>
    </Dialog>
    <DataLinkValidationDialog :validation="selectedValidation" @close="selectedValidation = null" />
  </section>
</template>

<style scoped>
.datalink-workspace { min-width: 0; margin-top: 14px; color: var(--workspace-text); font-size: 12px; }.workspace-chrome { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-bottom: 16px; border-bottom: 1px solid var(--workspace-border); }.workspace-tabs { display: flex; gap: 20px; min-width: 0; overflow-x: auto; }.workspace-tabs button { display: inline-flex; align-items: center; gap: 7px; min-height: 42px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--workspace-text-muted); cursor: pointer; font-size: 12px; white-space: nowrap; }.workspace-tabs button[aria-selected="true"] { border-bottom-color: var(--workspace-focus); color: var(--workspace-text); }.workspace-tabs button:hover { color: var(--workspace-text); }.workspace-chrome > :last-child { flex-shrink: 0; margin-bottom: 8px; }
.list-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }.catalog-search { display: flex; align-items: center; gap: 6px; flex: 1; color: var(--workspace-text-muted); min-width: 0; }.catalog-search :deep(input) { height: 34px; font-size: 12px; }.kind-filter { flex-shrink: 0; }select { min-height: 34px; width: 100%; border: 1px solid var(--workspace-border); border-radius: 6px; padding: 5px 9px; background: var(--workspace-surface-inset); color: var(--workspace-text); font-size: 12px; }select:focus-visible, button:focus-visible { outline: 2px solid var(--workspace-focus); outline-offset: 2px; }
.field-group { margin-bottom: 18px; }.field-group h3 { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 0 0 8px; font-size: 12px; font-weight: 600; color: var(--workspace-text-muted); }.field-group h3 span { font-weight: 500; }.catalog-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; border-bottom: 1px solid var(--workspace-border); padding: 4px 0; }.catalog-head, .catalog-row-main { display: grid; grid-template-columns: minmax(110px, .9fr) minmax(110px, .9fr) minmax(160px, 1.4fr) minmax(90px, .8fr) 16px; gap: 16px; align-items: center; }.catalog-head.relation-head, .catalog-row-main.relation-row-main { grid-template-columns: minmax(100px, .85fr) minmax(100px, .85fr) minmax(110px, .9fr) minmax(140px, 1.15fr) minmax(100px, .85fr) 16px; }.catalog-head { padding: 8px 12px; font-size: 10px; color: var(--workspace-text-muted); border-bottom: 1px solid var(--workspace-border); }.catalog-row-main { width: 100%; min-height: 56px; border: 0; background: transparent; color: var(--workspace-text); padding: 10px 12px; text-align: left; cursor: pointer; font-size: 12px; }.catalog-row-main.relation-row-main { min-height: 44px; padding: 8px 12px; }.catalog-row:hover, .concept-card:hover { background: var(--workspace-surface-hover); }.node-name, .node-concept, .node-description, .node-entity, .mapped-columns, .relation-kind, .node-other, .node-other-concept { display: grid; min-width: 0; gap: 3px; overflow-wrap: anywhere; }.node-name strong, .relation-kind strong { font-weight: 600; }.node-ref { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, "Cascadia Code", "Source Code Pro", monospace; font-size: 11px; }.node-name small, .node-description small, .node-entity, .mapped-columns, .muted, .relation-kind small { color: var(--workspace-text-muted); font-size: 11px; }.node-description { line-height: 1.6; }.concept-list { display: grid; gap: 10px; }.concept-card { display: grid; gap: 6px; width: 100%; border: 1px solid var(--workspace-border); border-radius: 8px; background: transparent; color: var(--workspace-text); padding: 12px; text-align: left; cursor: pointer; }
.join-label { color: var(--workspace-tool-python); }
.relation-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }.relation-actions:not(:has(button)) { display: none; }
.relation-endpoints { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; margin-bottom: 16px; }
.relation-endpoint { display: grid; gap: 4px; min-width: 0; }
.endpoint-label { color: var(--workspace-text-muted); font-size: 10px; }
.endpoint-mapping { color: var(--workspace-text-muted); font-size: 11px; overflow-wrap: anywhere; }
.endpoint-mapping small { color: var(--workspace-text-muted); }
.validation-list { display: grid; gap: 8px; margin-top: 16px; }
.validation-row { display: grid; gap: 4px; width: 100%; min-width: 0; border: 0; border-bottom: 1px solid var(--workspace-border); background: transparent; color: var(--workspace-text); padding: 10px 0; text-align: left; cursor: pointer; }
.validation-row small { color: var(--workspace-text-muted); overflow-wrap: anywhere; }
.status-running { color: var(--workspace-state-running); }
.status-completed { color: var(--workspace-state-succeeded); }
.status-partial { color: var(--workspace-state-warning); }
.status-failed, .status-canceled, .status-interrupted { color: var(--workspace-state-error); }
.list-pagination { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 0; color: var(--workspace-text-muted); font-size: 11px; }.list-pagination > div { display: flex; align-items: center; gap: 10px; }.list-pagination :deep(button) { width: 30px; height: 30px; }.panel-state { display: grid; min-height: 200px; place-items: center; margin: 0; color: var(--workspace-text-muted); text-align: center; }.panel-error { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; color: var(--workspace-state-error); overflow-wrap: anywhere; padding: 12px 0; }
:deep(.preview-dialog) { width: min(94vw, 960px); }.preview-layout { display: grid; grid-template-columns: minmax(220px, .8fr) minmax(0, 1.5fr); gap: 24px; }.preview-form { display: grid; align-content: start; gap: 14px; padding-right: 24px; border-right: 1px solid var(--workspace-border); }.preview-form label { display: grid; gap: 7px; font-size: 11px; color: var(--workspace-text-muted); }.preview-form :deep(textarea) { font-size: 12px; min-height: 110px; }.preview-options { display: grid; grid-template-columns: minmax(0, 1fr) 78px; gap: 10px; }.preview-options :deep(input) { height: 34px; }.preview-content { min-width: 0; }.preview-result-heading { display: flex; flex-wrap: wrap; gap: 8px; justify-content: space-between; margin-bottom: 16px; }.preview-result-heading span { font-size: 10px; color: var(--workspace-text-muted); }.truncation { color: var(--workspace-state-warning); }
:deep(.datalink-detail) { width: min(92vw, 760px); }.detail-heading { display: flex; align-items: start; justify-content: space-between; gap: 14px; }.detail-heading :deep(h2) { overflow-wrap: anywhere; font-size: 17px; }.detail-heading :deep(button) { flex-shrink: 0; }.detail-metadata { display: grid; gap: 14px; font-size: 12px; margin: 0; }.detail-metadata > div { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 12px; }.detail-metadata dt { color: var(--workspace-text-muted); }.detail-metadata dd { margin: 0; overflow-wrap: anywhere; line-height: 1.6; }.detail-metadata dd small { color: var(--workspace-text-muted); margin-left: 8px; }h4 { font-size: 12px; margin: 8px 0; }.mapping-row { display: grid; gap: 6px; padding: 12px 0; border-bottom: 1px solid var(--workspace-border); font-size: 12px; overflow-wrap: anywhere; }.mapping-row small { color: var(--workspace-text-muted); margin-left: 8px; }
.extra-entities { display: block; margin: 4px 0 0; color: var(--workspace-text-muted); }
@media (max-width: 850px) { .preview-layout { grid-template-columns: minmax(0, 1fr); }.preview-form { border-right: 0; border-bottom: 1px solid var(--workspace-border); padding: 0 0 18px; }.catalog-head, .catalog-row-main { grid-template-columns: minmax(90px, .8fr) minmax(90px, .8fr) minmax(120px, 1.2fr) 16px; gap: 10px; }.catalog-head:not(.relation-head) > :nth-child(4), .node-entity { display: none; }.catalog-head.relation-head, .catalog-row-main.relation-row-main { grid-template-columns: minmax(90px, .8fr) minmax(90px, .8fr) minmax(90px, .7fr) minmax(120px, 1fr) 16px; }.catalog-head.relation-head > :nth-child(5), .node-other-concept { display: none; } }
@media (max-width: 680px) { .workspace-chrome { flex-direction: row; align-items: center; gap: 8px; }.workspace-tabs { flex: 1 1 0; min-width: 0; overflow-x: auto; justify-content: flex-start; }.workspace-tabs button { flex: 0 0 auto; min-height: 44px; min-width: 44px; }.workspace-chrome > :last-child { margin-bottom: 0; align-self: center; flex-shrink: 0; min-height: 44px; }.catalog-row > :last-child { min-height: 44px; }.catalog-search :deep(input) { height: 44px; }.list-toolbar :deep(button) { min-height: 44px; min-width: 44px; }.list-pagination :deep(button) { width: 44px; height: 44px; }.relation-actions :deep(button) { min-height: 44px; } }
@media (max-width: 560px) { .workspace-tabs { gap: 0; }.workspace-tabs button { padding: 0 8px; font-size: 12px; gap: 4px; }.workspace-tabs svg { width: 13px; }.list-toolbar { gap: 6px; flex-wrap: wrap; }.catalog-search { min-width: 150px; }.catalog-head { display: none; }.catalog-row { grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 6px; }.catalog-row-main { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 16px; gap: 5px; min-height: 44px; padding: 8px; }.node-name { grid-column: 1; }.node-concept { grid-column: 2; }.node-description { grid-column: 1 / 3; font-size: 11px; }.catalog-row-main > svg { grid-column: 3; grid-row: 1 / 3; }.catalog-row-main.relation-row-main { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 16px; }.catalog-row-main.relation-row-main .relation-kind { grid-column: 1 / 3; }.catalog-row-main.relation-row-main .node-other { grid-column: 1; }.catalog-row-main.relation-row-main .node-other-concept { display: grid; grid-column: 2; }.catalog-row-main.relation-row-main > svg { grid-column: 3; grid-row: 1 / 3; }.relation-endpoints { grid-template-columns: minmax(0, 1fr); }.detail-metadata > div { grid-template-columns: minmax(0, 1fr); gap: 4px; } }
</style>
