import { ref } from "vue";
import { defineStore } from "pinia";

import {
  deleteDatasource,
  createDatasourceConnection,
  getDatasourceDatalinkGraph,
  getDatasourceDatalinkStatus,
  getDatasourceSchema,
  listDatasources,
  previewDatasourceTable,
  rebuildDatasourceDatalink,
  retryDatasource,
  updateDatasourceDescription,
  updateDatasourceMaskFields,
  uploadDatasource,
  updateDatasourceConnection,
} from "@/api/datasources";
import type {
  DataLinkGraph,
  DataLinkStatus,
  DataSource,
  SchemaSummary,
  TableData,
} from "@/api/types";

export const useDatasourceStore = defineStore("datasource", () => {
  const items = ref<DataSource[]>([]);
  const schemas = ref<Record<string, SchemaSummary>>({});
  const previews = ref<Record<string, TableData>>({});
  const datalinkStatuses = ref<Record<string, DataLinkStatus>>({});
  const datalinkGraphs = ref<Record<string, DataLinkGraph>>({});
  const loading = ref(false);
  const error = ref<string | null>(null);

  function replace(item: DataSource): void {
    items.value = [item, ...items.value.filter((candidate) => candidate.id !== item.id)];
  }

  async function load(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      items.value = (await listDatasources()).items;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : "数据源读取失败";
    } finally {
      loading.value = false;
    }
  }

  async function upload(file: File, type: string, name: string, description?: string): Promise<DataSource> {
    const item = await uploadDatasource(file, type, name, description);
    replace(item);
    return item;
  }

  async function createConnection(body: unknown): Promise<DataSource> {
    const item = await createDatasourceConnection(body); replace(item); return item;
  }

  async function updateConnection(id: string, body: unknown): Promise<DataSource> {
    const item = await updateDatasourceConnection(id, body); replace(item); return item;
  }

  async function refresh(datasourceId: string): Promise<DataSource> {
    const item = await retryDatasource(datasourceId);
    replace(item);
    return item;
  }

  async function updateMaskFields(datasourceId: string, maskFields: string[]): Promise<DataSource> {
    const item = await updateDatasourceMaskFields(datasourceId, maskFields);
    replace(item);
    return item;
  }

  async function updateDescription(datasourceId: string, description: string | null): Promise<DataSource> {
    const item = await updateDatasourceDescription(datasourceId, description);
    replace(item);
    return item;
  }

  async function loadSchema(datasourceId: string): Promise<SchemaSummary> {
    const schema = await getDatasourceSchema(datasourceId);
    schemas.value = { ...schemas.value, [datasourceId]: schema };
    return schema;
  }

  async function preview(datasourceId: string, tableName: string): Promise<TableData> {
    const data = await previewDatasourceTable(datasourceId, tableName);
    previews.value = { ...previews.value, [`${datasourceId}:${tableName}`]: data };
    return data;
  }

  async function loadDatalinkStatus(datasourceId: string): Promise<DataLinkStatus> {
    const status = await getDatasourceDatalinkStatus(datasourceId);
    datalinkStatuses.value = { ...datalinkStatuses.value, [datasourceId]: status };
    return status;
  }

  async function loadDatalinkGraph(datasourceId: string, graphVersion?: string): Promise<DataLinkGraph> {
    const graph = await getDatasourceDatalinkGraph(datasourceId, graphVersion);
    datalinkGraphs.value = { ...datalinkGraphs.value, [datasourceId]: graph };
    return graph;
  }

  async function rebuildDatalink(datasourceId: string): Promise<void> {
    await rebuildDatasourceDatalink(datasourceId);
    await loadDatalinkStatus(datasourceId);
  }

  async function remove(datasourceId: string): Promise<void> {
    const result = await deleteDatasource(datasourceId);
    items.value = items.value.map((item) =>
      item.id === datasourceId ? { ...item, status: result.status } : item,
    );
  }

  return {
    items,
    schemas,
    previews,
    datalinkStatuses,
    datalinkGraphs,
    loading,
    error,
    load,
    upload,
    createConnection,
    updateConnection,
    refresh,
    updateMaskFields,
    updateDescription,
    loadSchema,
    preview,
    loadDatalinkStatus,
    loadDatalinkGraph,
    rebuildDatalink,
    remove,
  };
});
