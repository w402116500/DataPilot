<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import {
  ArrowLeft,
  ArrowRight,
  Database,
  Eye,
  FileSpreadsheet,
  FileUp,
  ListTree,
  Network,
  Pencil,
  RefreshCw,
  RotateCw,
  TableProperties,
  Trash2,
} from "@lucide/vue";

import {
  getDatalinkCatalog,
  getDatasourceDatalinkSubgraph,
  listDatasourceTypes,
} from "@/api/datasources";
import type {
  DataLinkCatalog,
  DataLinkCatalogItem,
  DataLinkEdgeType,
  DataLinkNodeType,
  DataLinkSubgraph,
  DataSourceStatus,
  DataSourceTypeDescriptor,
  SchemaColumn,
  SchemaTable,
} from "@/api/types";
import { paginateTableGroups } from "@/lib/datalinkDisplay";
import { semanticSubgraphEdgeTypes } from "@/lib/datalinkSemanticMap";
import DataLinkGraphOverlay from "@/components/DataLinkGraphOverlay.vue";
import DataLinkGraphPanel from "@/components/DataLinkGraphPanel.vue";
import DataLinkWorkspace from "@/components/DataLinkWorkspace.vue";
import RichMarkdown from "@/components/RichMarkdown.vue";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { useDatasourceStore } from "@/stores/datasourceStore";

const datasources = useDatasourceStore();
const datalinkWorkspace = ref<InstanceType<typeof DataLinkWorkspace> | null>(null);
function confirmLeaveDatalink(): boolean {
  return !datalinkWorkspace.value?.hasUnsavedChanges || window.confirm("DataLink 有尚未保存的修改，确定离开吗？");
}
onBeforeRouteLeave(confirmLeaveDatalink);

const datasourceTypes = ref<DataSourceTypeDescriptor[]>([]);
const selectedDatasourceId = ref("");
const selectedTableName = ref("");
const activeTab = ref("schema");
const selectedFile = ref<File | null>(null);
const uploadType = ref("");
const uploadName = ref("");
const uploadDescription = ref("");
const connectionHost = ref("127.0.0.1");
const connectionPort = ref(3306);
const connectionDatabase = ref("");
const connectionUsername = ref("");
const connectionPassword = ref("");
const connectionTls = ref(false);
const connectionTimeout = ref(10);
const fileInput = ref<HTMLInputElement | null>(null);
const maskFieldsDraft = ref<string[]>([]);
const schemaPage = ref(1);
const editingDescription = ref(false);
const descriptionDraft = ref("");
const deleteOpen = ref(false);
const busyAction = ref<string | null>(null);
const actionError = ref<string | null>(null);
let pollingTimer: number | null = null;
const uploadPanelOpen = ref(true);
let uploadPanelQuery: MediaQueryList | null = null;

function onUploadPanelToggle(event: Event): void {
  const target = event.currentTarget;
  if (target instanceof HTMLDetailsElement) {
    uploadPanelOpen.value = target.open;
  }
}

function syncUploadPanelOpen(query: MediaQueryList | MediaQueryListEvent): void {
  uploadPanelOpen.value = !query.matches;
}

const enabledTypes = computed(() => datasourceTypes.value.filter((item) => item.enabled));
const selectedDatasource = computed(
  () => datasources.items.find((item) => item.id === selectedDatasourceId.value) ?? null,
);
const selectedSchema = computed(() =>
  selectedDatasource.value === null ? null : (datasources.schemas[selectedDatasource.value.id] ?? selectedDatasource.value.schema),
);
const schemaPages = computed(() =>
  paginateTableGroups(
    (selectedSchema.value?.tables ?? []).map((table) => ({ table: table.name, items: table.columns, source: table })),
  ),
);
const schemaPageCount = computed(() => Math.max(1, schemaPages.value.length));
const schemaColumnCount = computed(
  () => selectedSchema.value?.tables.reduce((total, table) => total + table.columns.length, 0) ?? 0,
);
const visibleSchemaTables = computed(() => {
  const pageIndex = Math.min(schemaPage.value, schemaPages.value.length) - 1;
  return (schemaPages.value[pageIndex] ?? []).map((group) => group.source);
});
const selectedPreview = computed(() => {
  const datasource = selectedDatasource.value;
  if (datasource === null || !selectedTableName.value) return null;
  return datasources.previews[`${datasource.id}:${selectedTableName.value}`] ?? null;
});
const selectedDatalinkStatus = computed(() =>
  selectedDatasource.value === null ? null : (datasources.datalinkStatuses[selectedDatasource.value.id] ?? null),
);
const selectedGraphVersion = computed(() =>
  selectedDatasource.value?.datalink_graph_version ?? selectedDatalinkStatus.value?.current_graph_version ?? null,
);
const selectedUploadType = computed(
  () => enabledTypes.value.find((item) => item.type === uploadType.value) ?? null,
);
const isConnectionType = computed(() => selectedUploadType.value?.upload_mode === "connection");
const canReadSchema = computed(() => {
  const status = selectedDatasource.value?.status;
  return status === "schema_ready" || status === "building_datalink" || status === "ready";
});
const isDatalinkRebuilding = computed(() => busyAction.value === "datalink-rebuild");
const runningDatalinkDatasourceIds = computed(() =>
  Object.entries(datasources.datalinkStatuses)
    .filter(([, status]) => status.current_build?.status === "running")
    .map(([datasourceId]) => datasourceId),
);
const hasTransientDatasources = computed(() =>
  runningDatalinkDatasourceIds.value.length > 0
  || datasources.items.some((item) =>
    ["uploaded", "inspecting", "schema_ready", "building_datalink", "deleting"].includes(item.status),
  ),
);
const datalinkCatalog = ref<DataLinkCatalog | null>(null);
const datalinkEntityIndex = ref<DataLinkCatalogItem[]>([]);
const datalinkEntityIndexVersion = ref<string | null>(null);
const datalinkSubgraph = ref<DataLinkSubgraph | null>(null);
const datalinkEntryType = ref<Extract<DataLinkNodeType, "concept" | "entity">>("entity");
const datalinkEntryQuery = ref("");
const datalinkEdgeTypes = ref<DataLinkEdgeType[]>([]);
const datalinkRootNodeId = ref<string | null>(null);
const datalinkInitialRootNodeId = ref<string | null>(null);
const datalinkCatalogLoading = ref(false);
const datalinkSubgraphLoading = ref(false);
const datalinkBrowserError = ref<string | null>(null);
const graphOverlayOpen = ref(false);
const datalinkRootName = computed(() => {
  const rootNodeId = datalinkRootNodeId.value;
  if (!rootNodeId) return null;
  return datalinkSubgraph.value?.nodes.find((node) => node.id === rootNodeId)?.name
    ?? datalinkCatalog.value?.items.find((item) => item.node.id === rootNodeId)?.node.name
    ?? null;
});
const graphPanelBindings = computed(() => ({
  catalog: datalinkCatalog.value,
  entityIndex: datalinkEntityIndex.value,
  subgraph: datalinkSubgraph.value,
  graphVersion: selectedGraphVersion.value,
  entryType: datalinkEntryType.value,
  entryQuery: datalinkEntryQuery.value,
  edgeTypes: datalinkEdgeTypes.value,
  currentRootNodeId: datalinkRootNodeId.value,
  initialRootNodeId: datalinkInitialRootNodeId.value,
  loadingCatalog: datalinkCatalogLoading.value,
  loadingSubgraph: datalinkSubgraphLoading.value,
  errorMessage: datalinkBrowserError.value,
}));
let datalinkSearchTimer: number | null = null;
let datalinkCatalogRequest = 0;
let datalinkEntityIndexRequest = 0;
let datalinkSubgraphRequest = 0;

function safeError(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message ? caught.message : fallback;
}

function statusLabel(status: DataSourceStatus): string {
  const labels: Record<DataSourceStatus, string> = {
    uploaded: "已上传",
    inspecting: "检查中",
    schema_ready: "Schema 已就绪",
    building_datalink: "数据地图构建中",
    ready: "可分析",
    failed: "准备失败",
    deleting: "删除中",
    deleted: "已删除",
  };
  return labels[status];
}

function statusClass(status: DataSourceStatus): string {
  if (status === "ready") return "state-success";
  if (["uploaded", "inspecting", "schema_ready", "building_datalink", "deleting"].includes(status)) {
    return "state-running";
  }
  if (status === "failed") return "state-error";
  return "state-muted";
}

function tableKey(tableName: string): string {
  return `${selectedDatasourceId.value}:${tableName}`;
}

function fileExtension(fileName: string): string {
  const extensionIndex = fileName.lastIndexOf(".");
  return extensionIndex > 0 ? fileName.slice(extensionIndex).toLowerCase() : "";
}

function formatCell(value: unknown): string {
  if (value === null) return "-";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "(非标量值)";
}

function columnConstraintMarks(table: SchemaTable, column: SchemaColumn): string[] {
  const marks: string[] = [];
  if (table.primary_key.includes(column.name)) marks.push("主键");
  if (!column.nullable) marks.push("非空");
  const foreignKey = table.foreign_keys.find((item) => item.columns.includes(column.name));
  if (foreignKey) marks.push(`外键 → ${foreignKey.referenced_table}`);
  return marks.length > 0 ? marks : ["—"];
}

function turnSchemaPage(delta: number): void {
  schemaPage.value = Math.min(schemaPageCount.value, Math.max(1, schemaPage.value + delta));
}

function setSelectedDatasource(datasourceId: string): void {
  if (datasourceId !== selectedDatasourceId.value && !confirmLeaveDatalink()) return;
  selectedDatasourceId.value = datasourceId;
  selectedTableName.value = "";
  activeTab.value = "schema";
  schemaPage.value = 1;
  maskFieldsDraft.value = [
    ...(datasources.items.find((item) => item.id === datasourceId)?.mask_fields ?? []),
  ];
  editingDescription.value = false;
  descriptionDraft.value = datasources.items.find((item) => item.id === datasourceId)?.description ?? "";
  actionError.value = null;
}

function pickInitialDatasource(): void {
  if (selectedDatasourceId.value && datasources.items.some((item) => item.id === selectedDatasourceId.value)) {
    return;
  }
  selectedDatasourceId.value = datasources.items.find((item) => item.status !== "deleted")?.id ?? "";
  selectedTableName.value = "";
}

async function perform<T>(action: string, fallback: string, operation: () => Promise<T>): Promise<T | undefined> {
  busyAction.value = action;
  actionError.value = null;
  try {
    return await operation();
  } catch (caught) {
    actionError.value = safeError(caught, fallback);
    return undefined;
  } finally {
    if (busyAction.value === action) busyAction.value = null;
  }
}

async function loadTypes(): Promise<void> {
  const result = await perform("types", "读取可用数据源类型失败", listDatasourceTypes);
  if (!result) return;
  datasourceTypes.value = result.items;
  if (!enabledTypes.value.some((item) => item.type === uploadType.value)) {
    uploadType.value = enabledTypes.value[0]?.type ?? "";
  }
}

async function refreshList(): Promise<void> {
  await perform("list", "刷新数据源失败", async () => {
    await datasources.load();
    if (datasources.error) throw new Error(datasources.error);
    pickInitialDatasource();
  });
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0] ?? null;
  selectedFile.value = file;
  if (file === null) return;
  if (!uploadName.value.trim()) {
    uploadName.value = file.name.replace(/\.[^.]+$/, "") || file.name;
  }
  const extension = fileExtension(file.name);
  const matchingType = enabledTypes.value.find((item) => item.accepted_extensions.includes(extension));
  if (matchingType) uploadType.value = matchingType.type;
}

async function upload(): Promise<void> {
  const file = selectedFile.value;
  const type = selectedUploadType.value;
  const name = uploadName.value.trim();
  if (!file || !type || !name) return;
  const extension = fileExtension(file.name);
  if (!type.accepted_extensions.includes(extension)) {
    actionError.value = `当前类型只接受 ${type.accepted_extensions.join("、")} 文件`;
    return;
  }
  const result = await perform("upload", "上传数据源失败", () =>
    datasources.upload(file, type.type, name, uploadDescription.value.trim() || undefined),
  );
  if (!result) return;
  setSelectedDatasource(result.id);
  selectedFile.value = null;
  uploadName.value = "";
  uploadDescription.value = "";
  if (fileInput.value) fileInput.value.value = "";
}

async function createConnection(): Promise<void> {
  const type = selectedUploadType.value;
  const name = uploadName.value.trim();
  if (!type || !name || !connectionHost.value.trim() || !connectionDatabase.value.trim()
    || !connectionUsername.value.trim() || !connectionPassword.value) {
    actionError.value = "请填写连接名称、主机、数据库、用户名和密码";
    return;
  }
  const result = await perform("connection", "创建 MySQL 连接失败", () =>
    datasources.createConnection({
      name,
      description: uploadDescription.value.trim() || null,
      type: type.type,
      config: {
        host: connectionHost.value.trim(),
        port: connectionPort.value,
        database: connectionDatabase.value.trim(),
        username: connectionUsername.value.trim(),
        tls: connectionTls.value,
        connect_timeout_seconds: connectionTimeout.value,
      },
      credentials: { password: connectionPassword.value },
    }),
  );
  if (!result) return;
  setSelectedDatasource(result.id);
  uploadName.value = "";
  uploadDescription.value = "";
  connectionPassword.value = "";
}

async function retryInspection(): Promise<void> {
  const datasource = selectedDatasource.value;
  if (!datasource) return;
  await perform("retry", "重新检查数据源失败", () => datasources.refresh(datasource.id));
}

async function loadSchema(): Promise<void> {
  const datasource = selectedDatasource.value;
  if (!datasource) return;
  const schema = await perform("schema", "读取 Schema 失败", () => datasources.loadSchema(datasource.id));
  if (schema && !selectedTableName.value) selectedTableName.value = schema.tables[0]?.name ?? "";
}

function startEditDescription(): void {
  descriptionDraft.value = selectedDatasource.value?.description ?? "";
  editingDescription.value = true;
}

function cancelEditDescription(): void {
  editingDescription.value = false;
  descriptionDraft.value = selectedDatasource.value?.description ?? "";
}

async function saveDescription(): Promise<void> {
  const datasource = selectedDatasource.value;
  if (!datasource) return;
  const saved = await perform("description", "保存数据源说明失败", () =>
    datasources.updateDescription(datasource.id, descriptionDraft.value.trim() || null),
  );
  if (saved) {
    editingDescription.value = false;
    descriptionDraft.value = saved.description ?? "";
  }
}

async function saveMaskFields(): Promise<void> {
  const datasource = selectedDatasource.value;
  if (!datasource) return;
  const saved = await perform("mask-fields", "保存遮蔽字段失败", () =>
    datasources.updateMaskFields(datasource.id, maskFieldsDraft.value),
  );
  if (saved) maskFieldsDraft.value = [...saved.mask_fields];
}

async function previewTable(tableName: string): Promise<void> {
  const datasource = selectedDatasource.value;
  if (!datasource) return;
  selectedTableName.value = tableName;
  activeTab.value = "preview";
  await perform(`preview:${tableKey(tableName)}`, "读取表预览失败", () =>
    datasources.preview(datasource.id, tableName),
  );
}

async function loadDatalinkStatus(): Promise<void> {
  const datasource = selectedDatasource.value;
  if (!datasource) return;
  await perform("datalink-status", "刷新数据地图状态失败", async () => {
    await datasources.loadDatalinkStatus(datasource.id);
    await datasources.load();
    if (datasources.error) throw new Error(datasources.error);
  });
}

async function rebuildDatalink(): Promise<void> {
  const datasource = selectedDatasource.value;
  if (!datasource) return;
  await perform("datalink-rebuild", "重建数据地图失败", async () => {
    await datasources.rebuildDatalink(datasource.id);
    await datasources.load();
    if (datasources.error) throw new Error(datasources.error);
  });
}

function clearDatalinkBrowser(): void {
  datalinkCatalogRequest += 1;
  datalinkEntityIndexRequest += 1;
  datalinkSubgraphRequest += 1;
  if (datalinkSearchTimer !== null) {
    window.clearTimeout(datalinkSearchTimer);
    datalinkSearchTimer = null;
  }
  datalinkCatalog.value = null;
  datalinkEntityIndex.value = [];
  datalinkEntityIndexVersion.value = null;
  datalinkSubgraph.value = null;
  datalinkEntryQuery.value = "";
  datalinkEdgeTypes.value = [];
  datalinkRootNodeId.value = null;
  datalinkInitialRootNodeId.value = null;
  datalinkBrowserError.value = null;
  datalinkCatalogLoading.value = false;
  datalinkSubgraphLoading.value = false;
  graphOverlayOpen.value = false;
}

function isCurrentDatalinkTarget(datasourceId: string, graphVersion: string): boolean {
  return selectedDatasource.value?.id === datasourceId && selectedGraphVersion.value === graphVersion;
}

async function loadDatalinkEntityIndex(datasourceId: string, graphVersion: string): Promise<void> {
  if (datalinkEntityIndexVersion.value === graphVersion) return;
  const requestId = ++datalinkEntityIndexRequest;
  const entities = await getDatalinkCatalog(datasourceId, {
    graphVersion,
    type: "entity",
    page: 1,
    pageSize: 100,
  });
  if (requestId !== datalinkEntityIndexRequest || !isCurrentDatalinkTarget(datasourceId, graphVersion)) return;
  datalinkEntityIndex.value = entities.items;
  datalinkEntityIndexVersion.value = graphVersion;
}

async function loadDatalinkEntries(selectInitialEntry = false): Promise<void> {
  const datasource = selectedDatasource.value;
  const graphVersion = selectedGraphVersion.value;
  if (!datasource || !graphVersion) return;
  const requestId = ++datalinkCatalogRequest;
  datalinkCatalogLoading.value = true;
  datalinkBrowserError.value = null;
  try {
    const catalogPromise = getDatalinkCatalog(datasource.id, {
      graphVersion,
      type: datalinkEntryType.value,
      query: datalinkEntryQuery.value,
      page: 1,
      pageSize: 100,
    });
    const [catalog] = await Promise.all([
      catalogPromise,
      datalinkEntryType.value === "concept"
        ? loadDatalinkEntityIndex(datasource.id, graphVersion)
        : Promise.resolve(),
    ]);
    if (requestId !== datalinkCatalogRequest || !isCurrentDatalinkTarget(datasource.id, graphVersion)) return;
    datalinkCatalog.value = catalog;
    if (selectInitialEntry && datalinkRootNodeId.value === null && catalog.items[0]) {
      await loadDatalinkSubgraph(catalog.items[0].node.id, true);
    }
  } catch (caught) {
    if (requestId === datalinkCatalogRequest && isCurrentDatalinkTarget(datasource.id, graphVersion)) {
      datalinkBrowserError.value = safeError(caught, "读取语义地图入口失败");
    }
  } finally {
    if (requestId === datalinkCatalogRequest) datalinkCatalogLoading.value = false;
  }
}

async function loadDatalinkSubgraph(rootNodeId: string, setInitialRoot = false): Promise<void> {
  const datasource = selectedDatasource.value;
  const graphVersion = selectedGraphVersion.value;
  if (!datasource || !graphVersion) return;
  const requestId = ++datalinkSubgraphRequest;
  const requestedEdgeTypes = [...datalinkEdgeTypes.value];
  datalinkSubgraphLoading.value = true;
  datalinkBrowserError.value = null;
  try {
    const subgraph = await getDatasourceDatalinkSubgraph(datasource.id, rootNodeId, {
      graphVersion,
      edgeTypes: semanticSubgraphEdgeTypes(requestedEdgeTypes),
      // 实体/属性入口一跳到语义邻居；第二跳经 represents 补齐详情中的物理字段。
      hops: 2,
    });
    if (requestId !== datalinkSubgraphRequest || !isCurrentDatalinkTarget(datasource.id, graphVersion)) return;
    datalinkSubgraph.value = subgraph;
    datalinkRootNodeId.value = rootNodeId;
    if (setInitialRoot || datalinkInitialRootNodeId.value === null) {
      datalinkInitialRootNodeId.value = rootNodeId;
    }
  } catch (caught) {
    if (requestId === datalinkSubgraphRequest && isCurrentDatalinkTarget(datasource.id, graphVersion)) {
      datalinkBrowserError.value = safeError(caught, "读取局部图失败");
    }
  } finally {
    if (requestId === datalinkSubgraphRequest) datalinkSubgraphLoading.value = false;
  }
}

async function openDatalinkBrowser(): Promise<void> {
  if (!selectedDatasource.value || !selectedGraphVersion.value) return;
  const isSameVersion = datalinkCatalog.value?.graph_version === selectedGraphVersion.value;
  if (!isSameVersion) clearDatalinkBrowser();
  await loadDatalinkEntries(!isSameVersion || datalinkRootNodeId.value === null);
}

async function refreshDatalinkBrowser(): Promise<void> {
  await loadDatalinkStatus();
  await openDatalinkBrowser();
}

async function selectDatalinkEntry(item: DataLinkCatalogItem): Promise<void> {
  await loadDatalinkSubgraph(item.node.id);
}

async function selectDatalinkEntryType(entryType: Extract<DataLinkNodeType, "concept" | "entity">): Promise<void> {
  if (datalinkEntryType.value === entryType) return;
  datalinkEntryType.value = entryType;
  await loadDatalinkEntries(false);
}

async function updateDatalinkEdgeTypes(edgeTypes: DataLinkEdgeType[]): Promise<void> {
  datalinkEdgeTypes.value = edgeTypes;
  if (datalinkRootNodeId.value) await loadDatalinkSubgraph(datalinkRootNodeId.value);
}

async function resetDatalinkRoot(): Promise<void> {
  if (datalinkInitialRootNodeId.value) await loadDatalinkSubgraph(datalinkInitialRootNodeId.value);
}

async function removeDatasource(): Promise<void> {
  const datasource = selectedDatasource.value;
  if (!datasource) return;
  const removed = await perform("delete", "删除数据源失败", async () => {
    await datasources.remove(datasource.id);
    return true;
  });
  if (removed === undefined) return;
  deleteOpen.value = false;
  await refreshList();
}

async function refreshTransientDatasources(): Promise<void> {
  if (!hasTransientDatasources.value || busyAction.value !== null) return;
  await Promise.all(
    runningDatalinkDatasourceIds.value.map(async (datasourceId) => {
      try {
        await datasources.loadDatalinkStatus(datasourceId);
      } catch {
        // 下一次轮询会再对账；临时网络错误不覆盖用户正在进行的其他操作。
      }
    }),
  );
  await refreshList();
}

onMounted(async () => {
  uploadPanelQuery = window.matchMedia("(max-width: 840px)");
  syncUploadPanelOpen(uploadPanelQuery);
  uploadPanelQuery.addEventListener("change", syncUploadPanelOpen);
  await Promise.all([refreshList(), loadTypes()]);
  pollingTimer = window.setInterval(() => {
    void refreshTransientDatasources();
  }, 5_000);
});

onBeforeUnmount(() => {
  if (pollingTimer !== null) window.clearInterval(pollingTimer);
  if (datalinkSearchTimer !== null) window.clearTimeout(datalinkSearchTimer);
  uploadPanelQuery?.removeEventListener("change", syncUploadPanelOpen);
});

watch(
  () => datasources.items,
  () => pickInitialDatasource(),
);

watch(schemaPageCount, (count) => {
  if (schemaPage.value > count) schemaPage.value = count;
});

watch(graphOverlayOpen, async (open, wasOpen) => {
  if (wasOpen && !open) {
    await nextTick();
    document.querySelector<HTMLButtonElement>('[title="全屏查看"]')?.focus();
  }
});

watch(selectedDatasourceId, () => {
  schemaPage.value = 1;
  clearDatalinkBrowser();
});

watch(selectedGraphVersion, (graphVersion, previousGraphVersion) => {
  if (graphVersion === previousGraphVersion) return;
  clearDatalinkBrowser();
  if (activeTab.value === "datalink" && graphVersion) void openDatalinkBrowser();
});

watch(activeTab, (tab) => {
  if (tab === "datalink") void refreshDatalinkBrowser();
});

watch(datalinkEntryQuery, () => {
  if (datalinkSearchTimer !== null) window.clearTimeout(datalinkSearchTimer);
  if (activeTab.value !== "datalink" || !selectedGraphVersion.value) return;
  datalinkSearchTimer = window.setTimeout(() => {
    datalinkSearchTimer = null;
    void loadDatalinkEntries(false);
  }, 220);
});
</script>

<template>
  <section class="datasources-view" aria-label="数据源管理">
    <header class="datasources-header">
      <div>
        <p class="section-kicker">数据准备</p>
        <h1>数据源</h1>
        <p>上传后会自动检查 Schema；Schema 就绪即可分析，数据地图按需手动构建。</p>
      </div>
      <Button variant="outline" :disabled="busyAction !== null" @click="refreshList">
        <RefreshCw :size="15" :class="{ spinning: busyAction === 'list' }" />刷新状态
      </Button>
    </header>

    <p v-if="actionError || datasources.error" class="page-error">{{ actionError ?? datasources.error }}</p>

    <div class="datasources-layout">
      <aside class="datasource-list-pane" aria-label="数据源列表">
        <details class="upload-panel" aria-labelledby="upload-title" :open="uploadPanelOpen" @toggle="onUploadPanelToggle">
          <summary class="panel-heading"><FileUp :size="16" /><h2 id="upload-title">添加数据源</h2></summary>
          <div class="upload-fields">
            <label class="field-label" for="datasource-type">类型</label>
            <select id="datasource-type" v-model="uploadType" class="field-select" :disabled="busyAction !== null || enabledTypes.length === 0">
              <option value="" disabled>选择类型</option>
              <option v-for="type in enabledTypes" :key="type.type" :value="type.type">{{ type.label }}</option>
            </select>
            <label v-if="!isConnectionType" class="field-label" for="datasource-file">文件</label>
            <input
              v-if="!isConnectionType"
              id="datasource-file"
              ref="fileInput"
              class="file-input"
              type="file"
              :accept="selectedUploadType?.accepted_extensions.join(',')"
              @change="onFileChange"
            >
            <p v-if="selectedFile" class="selected-file"><FileSpreadsheet :size="14" />{{ selectedFile.name }}</p>
            <label class="field-label" for="datasource-name">名称</label>
            <Input id="datasource-name" v-model="uploadName" :disabled="busyAction === 'upload'" maxlength="200" placeholder="例如：销售订单" />
            <p v-if="selectedUploadType" class="field-help">{{ selectedUploadType.description }}</p>
            <template v-if="isConnectionType">
              <label class="field-label" for="mysql-host">主机</label><Input id="mysql-host" v-model="connectionHost" placeholder="127.0.0.1" />
              <label class="field-label" for="mysql-port">端口</label><Input id="mysql-port" v-model.number="connectionPort" type="number" min="1" max="65535" />
              <label class="field-label" for="mysql-database">数据库</label><Input id="mysql-database" v-model="connectionDatabase" />
              <label class="field-label" for="mysql-username">用户名</label><Input id="mysql-username" v-model="connectionUsername" />
              <label class="field-label" for="mysql-password">密码</label><Input id="mysql-password" v-model="connectionPassword" type="password" autocomplete="new-password" />
              <label class="field-label" for="mysql-timeout">连接超时（秒）</label><Input id="mysql-timeout" v-model.number="connectionTimeout" type="number" min="1" max="30" />
              <label class="checkbox-row"><input v-model="connectionTls" type="checkbox"> 使用 TLS</label>
            </template>
            <label class="field-label" for="datasource-description">说明（可选）</label>
            <Textarea id="datasource-description" v-model="uploadDescription" :disabled="busyAction === 'upload'" placeholder="简短说明这份数据的用途" />
            <Button :disabled="(!isConnectionType && !selectedFile) || !uploadName.trim() || !uploadType || busyAction !== null" @click="isConnectionType ? createConnection() : upload()">
              <FileUp :size="15" />{{ isConnectionType ? '连接并检查' : (busyAction === 'upload' ? '正在上传' : '上传并检查') }}
            </Button>
          </div>
        </details>

        <div class="list-heading"><span>已保存数据源</span><small>{{ datasources.items.length }}</small></div>
        <div v-if="datasources.loading && datasources.items.length === 0" class="pane-empty">正在读取数据源...</div>
        <div v-else-if="datasources.items.length === 0" class="pane-empty">还没有数据源。上传 CSV 或 SQLite 文件开始准备。</div>
        <div v-else class="datasource-list">
          <button
            v-for="datasource in datasources.items"
            :key="datasource.id"
            class="datasource-row"
            :class="{ active: datasource.id === selectedDatasourceId }"
            type="button"
            @click="setSelectedDatasource(datasource.id)"
          >
            <span class="datasource-row-title"><Database :size="15" />{{ datasource.name }}</span>
            <span class="datasource-row-meta">{{ datasource.type.toUpperCase() }} · r{{ datasource.schema_revision }}</span>
            <Badge variant="outline" :class="statusClass(datasource.status)">{{ statusLabel(datasource.status) }}</Badge>
          </button>
        </div>
      </aside>

      <main class="datasource-detail-pane">
        <div v-if="selectedDatasource === null" class="detail-empty">
          <Database :size="30" stroke-width="1.4" />
          <h2>选择一个数据源</h2>
          <p>在左边上传或选择数据源，查看它是否已经能用于分析。</p>
        </div>
        <template v-else>
          <header class="detail-header">
            <div>
              <div class="detail-title-row"><h2>{{ selectedDatasource.name }}</h2><Badge variant="outline" :class="statusClass(selectedDatasource.status)">{{ statusLabel(selectedDatasource.status) }}</Badge></div>
              <div v-if="!editingDescription" class="description-view">
                <RichMarkdown v-if="selectedDatasource.description" :content="selectedDatasource.description" density="artifact" />
                <p v-else>没有补充说明</p>
                <Button
                  variant="outline"
                  size="sm"
                  :disabled="busyAction !== null || selectedDatasource.status === 'deleted' || selectedDatasource.status === 'deleting'"
                  @click="startEditDescription"
                ><Pencil :size="14" />编辑说明</Button>
              </div>
              <div v-else class="description-edit">
                <Textarea
                  v-model="descriptionDraft"
                  :maxlength="2000"
                  rows="5"
                  aria-label="数据源说明"
                  placeholder="用几段话说明这份数据的业务含义、口径或使用注意"
                />
                <div class="description-edit-actions">
                  <Button size="sm" :disabled="busyAction !== null" @click="saveDescription">{{ busyAction === 'description' ? '正在保存' : '保存说明' }}</Button>
                  <Button variant="ghost" size="sm" :disabled="busyAction !== null" @click="cancelEditDescription">取消</Button>
                </div>
              </div>
            </div>
            <div class="detail-actions">
              <Button variant="outline" size="sm" :disabled="busyAction !== null || selectedDatasource.status === 'deleted' || selectedDatasource.status === 'deleting'" @click="retryInspection"><RotateCw :size="14" />重新检查</Button>
              <Button variant="outline" size="sm" :disabled="busyAction !== null || selectedDatasource.status === 'deleted' || selectedDatasource.status === 'deleting'" @click="deleteOpen = true"><Trash2 :size="14" />删除</Button>
            </div>
          </header>

          <p v-if="selectedDatasource.status === 'failed'" class="detail-notice state-error">Schema 检查没有完成。可以重新检查；Schema 就绪后即可分析，数据地图按需手动构建。</p>
          <p v-else-if="selectedDatasource.status === 'deleted'" class="detail-notice">数据源已删除。相关历史审计和产物仍可查看，但不能再重新执行分析。</p>
          <p v-else-if="!canReadSchema" class="detail-notice state-running">文件正在准备中。页面会定时刷新状态，你也可以手动刷新。</p>
          <p v-else-if="!selectedDatasource.mask_fields_confirmed" class="detail-notice state-running">请确认需要遮蔽的字段，保存空清单也表示确认没有需要遮蔽的字段。</p>

          <Tabs :model-value="activeTab" class="detail-tabs" @update:model-value="(value) => { if (confirmLeaveDatalink()) activeTab = String(value); }">
            <TabsList class="detail-tabs-list">
              <TabsTrigger value="schema"><ListTree :size="14" />Schema</TabsTrigger>
              <TabsTrigger value="preview" :disabled="!canReadSchema"><Eye :size="14" />预览</TabsTrigger>
              <TabsTrigger value="datalink" :disabled="selectedDatasource.status === 'deleted'"><Network :size="14" />数据地图</TabsTrigger>
            </TabsList>

            <TabsContent value="schema" class="tab-panel">
              <div class="tab-actions">
                <div>
                  <h3>表结构</h3>
                  <p class="tab-help">读取的是检查完成后固化的 Schema。勾选需要遮蔽的列，系统不会自动猜测；保存空清单也表示确认没有需要遮蔽的字段。</p>
                </div>
                <div class="tab-action-buttons">
                  <Badge variant="outline">{{ selectedDatasource.mask_fields_confirmed ? '已确认' : '待确认' }}</Badge>
                  <Button variant="outline" size="sm" :disabled="busyAction !== null || !selectedSchema" @click="saveMaskFields">
                    {{ busyAction === 'mask-fields' ? '正在保存' : '保存确认' }}
                  </Button>
                  <Button variant="outline" size="sm" :disabled="!canReadSchema || busyAction !== null" @click="loadSchema"><RefreshCw :size="14" />读取 Schema</Button>
                </div>
              </div>
              <p v-if="!selectedSchema" class="tab-empty">{{ canReadSchema ? '点击“读取 Schema”查看表结构。' : '等待文件检查完成。' }}</p>
              <div v-else class="schema-list">
                <article v-for="table in visibleSchemaTables" :key="table.name" class="schema-table">
                  <header>
                    <div><TableProperties :size="15" /><strong>{{ table.name }}</strong></div>
                    <span>{{ table.row_count === null ? '行数未知' : `${table.row_count} 行` }}</span>
                  </header>
                  <div class="schema-catalog">
                    <div class="schema-head" aria-hidden="true"><span>遮蔽</span><span>字段</span><span>类型</span><span>约束</span></div>
                    <label
                      v-for="column in table.columns"
                      :key="column.name"
                      class="schema-row"
                    >
                      <input
                        v-model="maskFieldsDraft"
                        type="checkbox"
                        :value="column.name"
                        :disabled="busyAction !== null"
                        :aria-label="`遮蔽 ${table.name}.${column.name}`"
                      >
                      <span class="schema-column-name">{{ column.name }}</span>
                      <span class="schema-column-type">{{ column.type }}</span>
                      <span class="schema-column-marks">
                        <small v-for="mark in columnConstraintMarks(table, column)" :key="mark">{{ mark }}</small>
                      </span>
                    </label>
                  </div>
                  <Button variant="outline" size="sm" :disabled="busyAction !== null" @click="previewTable(table.name)"><Eye :size="14" />预览前 50 行</Button>
                </article>
                <div class="schema-pagination">
                  <span>{{ schemaColumnCount }} 列</span>
                  <div>
                    <Button variant="ghost" size="icon" :disabled="schemaPage <= 1" aria-label="上一页" title="上一页" @click="turnSchemaPage(-1)"><ArrowLeft :size="15" /></Button>
                    <span>{{ schemaPage }} / {{ schemaPageCount }}</span>
                    <Button variant="ghost" size="icon" :disabled="schemaPage >= schemaPageCount" aria-label="下一页" title="下一页" @click="turnSchemaPage(1)"><ArrowRight :size="15" /></Button>
                  </div>
                </div>
              </div>
            </TabsContent>

            <TabsContent value="preview" class="tab-panel">
              <div class="tab-actions"><div><h3>表预览</h3><p class="tab-help">预览最多返回 50 行，所有读取仍经过数据网关的安全边界。</p></div><Button variant="outline" size="sm" :disabled="!selectedTableName || busyAction !== null" @click="previewTable(selectedTableName)"><RefreshCw :size="14" />刷新预览</Button></div>
              <p v-if="!selectedTableName" class="tab-empty">先在 Schema 中选择一张表。</p>
              <p v-else-if="!selectedPreview" class="tab-empty">正在等待表预览。你可以在 Schema 页再次点击“预览前 50 行”。</p>
              <template v-else>
                <p class="preview-summary">{{ selectedTableName }} · 返回 {{ selectedPreview.row_count }} 行</p>
                <div class="preview-scroll"><table><thead><tr><th v-for="column in selectedPreview.columns" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="(row, rowIndex) in selectedPreview.rows" :key="rowIndex"><td v-for="(cell, columnIndex) in row" :key="columnIndex" :title="formatCell(cell)">{{ formatCell(cell) }}</td></tr><tr v-if="selectedPreview.rows.length === 0"><td :colspan="selectedPreview.columns.length || 1" class="empty-table">没有可显示的行</td></tr></tbody></table></div>
              </template>
            </TabsContent>

            <TabsContent value="datalink" class="tab-panel">
              <div class="tab-actions datalink-actions"><div><h3>DataLink</h3><p v-if="isDatalinkRebuilding">正在重新构建，旧版本仍可浏览</p></div><div class="tab-action-buttons"><Button variant="outline" size="sm" :disabled="busyAction !== null || datalinkCatalogLoading || datalinkSubgraphLoading" @click="refreshDatalinkBrowser"><RefreshCw :size="14" />刷新</Button><Button variant="outline" size="sm" :disabled="!canReadSchema || busyAction !== null" @click="rebuildDatalink"><RotateCw :size="14" :class="{ spinning: isDatalinkRebuilding }" />{{ isDatalinkRebuilding ? '正在重新构建' : '重新构建' }}</Button></div></div>
              <p v-if="selectedDatalinkStatus?.last_error_code || selectedDatasource.status === 'failed'" class="detail-notice state-error">数据地图暂时不可用。请刷新状态或重新构建。</p>
              <p v-else-if="!selectedGraphVersion" class="tab-empty">当前还没有完成的数据地图。可以重新构建，构建完成后再浏览实体、属性和语义地图。</p>
              <DataLinkWorkspace
                v-else
                ref="datalinkWorkspace"
                :datasource-id="selectedDatasource.id"
                :graph-version="selectedGraphVersion"
                :schema-revision="selectedDatasource.schema_revision"
                @published="refreshDatalinkBrowser"
                class="datalink-browser"
              >
                <template #graph>
                  <DataLinkGraphPanel
                    v-if="!graphOverlayOpen"
                    v-bind="graphPanelBindings"
                    allow-fullscreen
                    @select-entry="selectDatalinkEntry"
                    @update-entry-type="selectDatalinkEntryType"
                    @update-entry-query="datalinkEntryQuery = $event"
                    @update-edge-types="updateDatalinkEdgeTypes"
                    @focus-node="loadDatalinkSubgraph($event)"
                    @reset-root="resetDatalinkRoot"
                    @fullscreen="graphOverlayOpen = true"
                  />
                </template>
              </DataLinkWorkspace>
            </TabsContent>
          </Tabs>
        </template>
      </main>
    </div>

    <DataLinkGraphOverlay
      :open="graphOverlayOpen"
      :root-name="datalinkRootName"
      :graph-version="selectedGraphVersion"
      @update:open="graphOverlayOpen = $event"
    >
      <DataLinkGraphPanel
        v-if="graphOverlayOpen"
        v-bind="graphPanelBindings"
        mode="overlay"
        @select-entry="selectDatalinkEntry"
        @update-entry-type="selectDatalinkEntryType"
        @update-entry-query="datalinkEntryQuery = $event"
        @focus-node="loadDatalinkSubgraph($event)"
        @update-edge-types="updateDatalinkEdgeTypes"
        @reset-root="resetDatalinkRoot"
      />
    </DataLinkGraphOverlay>

    <Dialog v-model:open="deleteOpen">
      <DialogContent>
        <DialogHeader><DialogTitle>删除数据源？</DialogTitle><DialogDescription>会先停止仍在使用这份数据的运行任务，再删除数据源和数据地图。历史审计和产物保留可读，但不能再用这份数据重新分析。</DialogDescription></DialogHeader>
        <DialogFooter><Button variant="outline" :disabled="busyAction === 'delete'" @click="deleteOpen = false">取消</Button><Button variant="destructive" :disabled="busyAction === 'delete'" @click="removeDatasource"><Trash2 :size="14" />{{ busyAction === 'delete' ? '正在删除' : '确认删除' }}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </section>
</template>

<style scoped>
.datasources-view { min-height: calc(100vh - 48px); padding: 24px; }
.datasources-header, .detail-header, .tab-actions, .panel-heading, .detail-title-row, .datasource-row-title, .schema-table header, .schema-table header > div, .selected-file, .tab-action-buttons { display: flex; align-items: center; }
.datasources-header { justify-content: space-between; gap: 20px; max-width: 1440px; margin: 0 auto 18px; }
.section-kicker { margin: 0 0 5px; color: var(--muted-foreground); font-size: 11px; font-weight: 700; }
.datasources-header h1 { margin: 0; font-size: 22px; line-height: 1.25; }
.datasources-header p:last-child { margin: 7px 0 0; color: var(--muted-foreground); font-size: 13px; line-height: 1.5; }
.page-error { max-width: 1440px; margin: 0 auto 14px; border: 1px solid color-mix(in srgb, var(--destructive) 45%, var(--workspace-border)); background: color-mix(in srgb, var(--destructive) 12%, var(--workspace-surface-subtle)); padding: 9px 11px; color: var(--destructive); font-size: 13px; }
.datasources-layout { display: grid; max-width: 1440px; min-height: 690px; grid-template-columns: minmax(270px, 330px) minmax(0, 1fr); margin: 0 auto; overflow: hidden; border: 1px solid var(--border); border-radius: 6px; background: var(--card); }
.datasource-list-pane { display: flex; min-width: 0; min-height: 0; flex-direction: column; overflow: hidden; isolation: isolate; border-right: 1px solid var(--border); background: var(--workspace-surface-subtle); }
.upload-panel { border-bottom: 1px solid var(--border); padding: 14px; }
.panel-heading { gap: 7px; color: var(--workspace-text-muted); }.panel-heading h2 { margin: 0; font-size: 13px; }
.upload-panel > summary { display: flex; width: 100%; list-style: none; }
.upload-panel > summary::-webkit-details-marker, .upload-panel > summary::marker { display: none; content: none; }
.upload-fields { display: grid; gap: 7px; margin-top: 12px; }.field-label, .list-heading { color: var(--workspace-text-muted); font-size: 11px; font-weight: 700; }
.file-input, .field-select { width: 100%; min-height: 36px; border: 1px solid var(--input); border-radius: 8px; background: var(--workspace-surface-inset); padding: 6px 8px; color: var(--foreground); font-size: 12px; }
.selected-file { gap: 5px; overflow: hidden; color: var(--muted-foreground); font-size: 11px; }.selected-file svg { flex: 0 0 auto; }.selected-file { text-overflow: ellipsis; white-space: nowrap; }
.field-help { margin: 0; color: var(--muted-foreground); font-size: 11px; line-height: 1.45; }
.list-heading { display: flex; justify-content: space-between; padding: 14px 14px 8px; }.list-heading small { color: var(--muted-foreground); font-weight: 500; }
.datasource-list { display: grid; min-height: 0; flex: 1; align-content: start; gap: 2px; overflow-x: hidden; overflow-y: auto; padding: 0 8px 10px; }
.datasource-row { display: grid; width: 100%; gap: 4px; border: 0; border-radius: 8px; background: transparent; padding: 9px 7px; color: inherit; cursor: pointer; text-align: left; }.datasource-row:hover { background: var(--workspace-surface-hover); }.datasource-row.active { background: var(--workspace-surface-selected); }
.datasource-row-title { min-width: 0; gap: 6px; font-size: 13px; font-weight: 650; }.datasource-row-title svg { flex: 0 0 auto; }.datasource-row-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.datasource-row-meta { color: var(--muted-foreground); font-size: 11px; }.datasource-row .inline-flex { justify-self: start; }
.pane-empty { padding: 18px 14px; color: var(--muted-foreground); font-size: 12px; line-height: 1.55; }
.datasource-detail-pane { position: relative; z-index: 1; min-width: 0; background: var(--card); }.detail-empty { display: grid; height: 100%; min-height: 520px; place-content: center; justify-items: center; gap: 9px; padding: 24px; color: var(--muted-foreground); text-align: center; }.detail-empty h2, .detail-empty p { margin: 0; }.detail-empty h2 { color: var(--foreground); font-size: 17px; }.detail-empty p { max-width: 340px; font-size: 13px; line-height: 1.6; }
.detail-header { justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--border); padding: 18px 20px; }.detail-header > div:first-child { min-width: 0; flex: 1; }.detail-title-row { flex-wrap: wrap; gap: 8px; }.detail-title-row h2 { margin: 0; font-size: 17px; }.detail-header p { margin: 6px 0 0; color: var(--muted-foreground); font-size: 12px; }.detail-actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; gap: 7px; }.description-view, .description-edit { display: grid; gap: 8px; margin-top: 8px; min-width: 0; }.description-view > :last-child { justify-self: start; }.description-view :deep(.rich-markdown) { font-size: 12px; color: var(--muted-foreground); }.description-edit-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.detail-notice { margin: 14px 20px 0; border-left: 2px solid var(--workspace-border); background: var(--workspace-surface-subtle); padding: 9px 10px; color: var(--workspace-text-muted); font-size: 12px; line-height: 1.5; }.detail-notice.state-error { border-left-color: var(--destructive); }.detail-notice.state-running { border-left-color: var(--running); }
.detail-tabs { padding: 16px 20px 20px; }.detail-tabs-list { display: inline-flex; width: fit-content; max-width: 100%; min-height: 38px; overflow-x: auto; overflow-y: hidden; scrollbar-width: none; }.detail-tabs-list::-webkit-scrollbar { display: none; }.detail-tabs-list :deep(button) { flex: 0 0 auto; width: auto; min-width: max-content; padding-left: 8px; padding-right: 8px; }.detail-tabs-list :deep(button > span) { display: inline-flex; min-width: max-content; align-items: center; justify-content: center; gap: 5px; overflow: visible; text-overflow: clip; }.detail-tabs-list :deep(button > span svg) { flex: 0 0 auto; }.tab-panel { padding-top: 16px; }.tab-actions { justify-content: space-between; gap: 14px; }.tab-actions h3 { margin: 0; font-size: 14px; }.tab-actions p { margin: 5px 0 0; color: var(--muted-foreground); font-size: 12px; line-height: 1.45; }.tab-action-buttons { flex: 0 0 auto; gap: 7px; }
.tab-empty { margin: 26px 0; color: var(--muted-foreground); font-size: 13px; }
.schema-list { display: grid; gap: 18px; margin-top: 16px; }
.schema-table { min-width: 0; }
.schema-table header { justify-content: space-between; gap: 10px; margin-bottom: 4px; color: var(--workspace-text-muted); font-size: 12px; font-weight: 600; }
.schema-table header > div { gap: 6px; color: var(--workspace-text); font-size: 13px; }
.schema-head, .schema-row { display: grid; grid-template-columns: 36px minmax(148px, 220px) 108px minmax(160px, 1fr); gap: 12px; align-items: center; }
.schema-head { padding: 8px 12px; border-bottom: 1px solid var(--workspace-border); color: var(--workspace-text-muted); font-size: 10px; }
.schema-row { min-height: 40px; border-bottom: 1px solid var(--workspace-border); padding: 8px 12px; color: var(--workspace-text); font-size: 12px; cursor: pointer; }
.schema-row:hover { background: var(--workspace-surface-hover); }
.schema-row input { width: 14px; height: 14px; justify-self: start; accent-color: var(--primary); }
.schema-column-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.schema-column-type { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--workspace-text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
.schema-column-marks { display: flex; flex-wrap: wrap; gap: 6px; min-width: 0; }
.schema-column-marks small { color: var(--workspace-text-muted); font-size: 11px; }
.schema-table :deep(button) { margin-top: 9px; }
.schema-pagination { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding-top: 4px; color: var(--workspace-text-muted); font-size: 11px; }
.schema-pagination > div { display: flex; align-items: center; gap: 10px; }
.schema-pagination :deep(button) { width: 30px; height: 30px; }
.preview-summary { margin: 16px 0 8px; color: var(--workspace-text-muted); font-size: 12px; }
.preview-scroll { max-width: 100%; overflow: auto; border: 1px solid var(--workspace-border); background: var(--workspace-surface-inset); }
.preview-scroll table { width: 100%; min-width: max-content; border-collapse: collapse; font-size: 12px; }
.preview-scroll th, .preview-scroll td { max-width: 280px; border-bottom: 1px solid var(--workspace-border); padding: 8px 10px; text-align: left; vertical-align: top; }
.preview-scroll th { position: sticky; top: 0; background: var(--workspace-table-header); color: var(--workspace-text); font-weight: 700; }
.preview-scroll td { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-table { color: var(--workspace-text-muted); text-align: center !important; }
.datalink-browser { margin-top: 8px; }
.spinning { animation: spin 1s linear infinite; }@keyframes spin { to { transform: rotate(360deg); } }
@media (min-width: 841px) { .upload-panel > summary { pointer-events: none; cursor: default; } }
@media (max-width: 840px) { .datasources-view { padding: 12px; }.datasources-layout { min-height: 0; grid-template-columns: minmax(0, 1fr); }.datasource-list-pane { border-right: 0; border-bottom: 1px solid var(--border); }.upload-panel { display: block; }.upload-panel > summary { min-height: 44px; cursor: pointer; }.upload-panel > summary::after { content: ""; margin-left: auto; width: 7px; height: 7px; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; transform: rotate(45deg); }.upload-panel[open] > summary::after { transform: rotate(-135deg); translate: 0 3px; }.upload-fields { margin-top: 12px; }.datasource-list { flex: none; max-height: 230px; grid-template-columns: minmax(0, 1fr); }.detail-empty { min-height: 380px; }.detail-header { padding: 16px; }.detail-tabs { padding: 14px 16px 18px; }.detail-notice { margin-left: 16px; margin-right: 16px; }.detail-tabs-list { min-height: 44px; }.detail-tabs-list :deep(button) { min-height: 44px; height: 44px; }.detail-actions :deep(button), .tab-action-buttons :deep(button), .schema-table :deep(button) { min-height: 44px; }.schema-row { min-height: 44px; }.schema-head { display: none; }.schema-row { grid-template-columns: 44px minmax(0, 1fr) auto; grid-template-areas: "check name type" "check marks marks"; align-items: center; gap: 2px 8px; }.schema-row input { grid-area: check; }.schema-column-name { grid-area: name; }.schema-column-type { grid-area: type; }.schema-column-marks { grid-area: marks; }.schema-pagination :deep(button) { width: 44px; height: 44px; } }
@media (max-width: 560px) { .datasources-view { padding: 10px; }.datasources-header { align-items: flex-start; gap: 12px; }.datasources-header h1 { font-size: 19px; }.datasources-header > :last-child { flex: 0 0 auto; }.datasources-header > :last-child :deep(span) { display: none; }.upload-panel { display: block; }.upload-fields { margin-top: 12px; }.datasource-list { flex: none; grid-template-columns: minmax(0, 1fr); max-height: 160px; }.detail-header { padding: 12px; gap: 10px; }.detail-header, .tab-actions { align-items: flex-start; flex-direction: column; }.detail-actions, .tab-action-buttons { width: 100%; }.detail-actions :deep(button), .tab-action-buttons :deep(button) { flex: 1; min-height: 44px; }.description-view :deep(.rich-markdown) { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }.description-view > :last-child { width: max-content; }.tab-actions { gap: 8px; }.tab-help { display: none; }.datalink-actions h3 { display: none; }.datalink-actions > div:first-child:not(:has(p)) { display: none; }.schema-head { display: none; }.schema-row { grid-template-columns: 44px minmax(0, 1fr) auto; grid-template-areas: "check name type" "check marks marks"; align-items: center; gap: 2px 8px; min-height: 44px; padding: 8px 4px; }.schema-row input { grid-area: check; width: 18px; height: 18px; margin: 0; justify-self: center; }.schema-column-name { grid-area: name; }.schema-column-type { grid-area: type; grid-column: auto; }.schema-column-marks { grid-area: marks; grid-column: auto; }.schema-pagination :deep(button) { width: 44px; height: 44px; }.graph-summary { grid-template-columns: repeat(3, minmax(0, 1fr)); }.graph-summary div { padding: 9px 4px; }.detail-tabs-list { width: 100%; min-height: 44px; }.detail-tabs-list :deep(button) { min-height: 44px; height: 44px; padding-left: 8px; padding-right: 8px; } }
</style>
