import { requestJson } from "./client";
import type {
  DataLinkEdgeType,
  DataLinkCatalog,
  DataLinkCatalogDetail,
  DataLinkNodeType,
  DataLinkRelations,
  DataLinkPreview,
  DataLinkPreviewRequest,
  DataLinkDraft,
  DataLinkDraftChange,
  DataLinkPublishResult,
  DataLinkDraftPreview,
  DataLinkDraftPreviewRequest,
  DataLinkVersions,
  DataLinkVersionDiff,
  DataLinkRestoreRequest,
  DataLinkVersionPublish,
  DataLinkRebuildConflicts,
  DataLinkResolveCandidateRequest,
  DataLinkValidation,
  DataLinkValidationRequest,
  DataLinkGraphEntries,
  DataLinkGraphEntryType,
  DataLinkGraph,
  DataLinkRebuildResult,
  DataLinkStatus,
  DataLinkSubgraph,
  DataSource,
  DataSourceDeleteResult,
  DataSourceTypeDescriptor,
  PageResult,
  SchemaSummary,
  TableData,
} from "./types";

function pageQuery(page: number, pageSize: number): string {
  return `?page=${page}&page_size=${pageSize}`;
}

export function listDatasourceTypes(): Promise<{ items: DataSourceTypeDescriptor[] }> {
  return requestJson<{ items: DataSourceTypeDescriptor[] }>("/datasource-types");
}

export function listDatasources(page = 1, pageSize = 20): Promise<PageResult<DataSource>> {
  return requestJson<PageResult<DataSource>>(`/datasources${pageQuery(page, pageSize)}`);
}

export function uploadDatasource(
  file: File,
  type: string,
  name: string,
  description?: string,
): Promise<DataSource> {
  const body = new FormData();
  body.append("file", file, file.name);
  body.append("type", type);
  body.append("name", name);
  if (description !== undefined) {
    body.append("description", description);
  }
  return requestJson<DataSource>("/datasources/upload", { method: "POST", body });
}

export function createDatasourceConnection(body: unknown): Promise<DataSource> {
  return requestJson<DataSource>("/datasources/connections", { method: "POST", body: JSON.stringify(body) });
}

export function updateDatasourceConnection(datasourceId: string, body: unknown): Promise<DataSource> {
  return requestJson<DataSource>(`/datasources/${encodeURIComponent(datasourceId)}/connection`, { method: "PATCH", body: JSON.stringify(body) });
}

export function getDatasource(datasourceId: string): Promise<DataSource> {
  return requestJson<DataSource>(`/datasources/${encodeURIComponent(datasourceId)}`);
}

export function retryDatasource(datasourceId: string): Promise<DataSource> {
  return requestJson<DataSource>(`/datasources/${encodeURIComponent(datasourceId)}/test`, {
    method: "POST",
  });
}

export function updateDatasourceMaskFields(
  datasourceId: string,
  maskFields: string[],
): Promise<DataSource> {
  return requestJson<DataSource>(`/datasources/${encodeURIComponent(datasourceId)}/mask-fields`, {
    method: "PATCH",
    body: JSON.stringify({ mask_fields: maskFields }),
  });
}

export function updateDatasourceDescription(
  datasourceId: string,
  description: string | null,
): Promise<DataSource> {
  return requestJson<DataSource>(`/datasources/${encodeURIComponent(datasourceId)}/description`, {
    method: "PATCH",
    body: JSON.stringify({ description }),
  });
}

export function getDatasourceSchema(datasourceId: string): Promise<SchemaSummary> {
  return requestJson<SchemaSummary>(`/datasources/${encodeURIComponent(datasourceId)}/schema`);
}

export function previewDatasourceTable(
  datasourceId: string,
  tableName: string,
  limit = 50,
): Promise<TableData> {
  return requestJson<TableData>(
    `/datasources/${encodeURIComponent(datasourceId)}/tables/${encodeURIComponent(tableName)}/preview?limit=${limit}`,
  );
}

export function deleteDatasource(datasourceId: string): Promise<DataSourceDeleteResult> {
  return requestJson<DataSourceDeleteResult>(`/datasources/${encodeURIComponent(datasourceId)}`, {
    method: "DELETE",
  });
}

export function rebuildDatasourceDatalink(datasourceId: string): Promise<DataLinkRebuildResult> {
  return requestJson<DataLinkRebuildResult>(
    `/datasources/${encodeURIComponent(datasourceId)}/datalink/rebuild`,
    { method: "POST" },
  );
}

export function getDatasourceDatalinkStatus(datasourceId: string): Promise<DataLinkStatus> {
  return requestJson<DataLinkStatus>(
    `/datasources/${encodeURIComponent(datasourceId)}/datalink/status`,
  );
}

export function getDatasourceDatalinkGraph(
  datasourceId: string,
  graphVersion?: string,
): Promise<DataLinkGraph> {
  const query = graphVersion === undefined ? "" : `?graph_version=${encodeURIComponent(graphVersion)}`;
  return requestJson<DataLinkGraph>(
    `/datasources/${encodeURIComponent(datasourceId)}/datalink/graph${query}`,
  );
}

export interface DataLinkEntriesQuery {
  graphVersion?: string;
  entryType?: DataLinkGraphEntryType;
  query?: string;
  page?: number;
  pageSize?: number;
}

export interface DataLinkSubgraphQuery {
  graphVersion?: string;
  edgeTypes?: DataLinkEdgeType[];
  hops?: number;
}

function datalinkBrowserQuery(
  values: Record<string, string | number | undefined>,
  edgeTypes: DataLinkEdgeType[] = [],
): string {
  const query = new URLSearchParams();
  for (const [name, value] of Object.entries(values)) {
    if (value !== undefined) query.set(name, String(value));
  }
  for (const edgeType of edgeTypes) query.append("edge_types", edgeType);
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export function getDatasourceDatalinkEntries(
  datasourceId: string,
  options: DataLinkEntriesQuery = {},
): Promise<DataLinkGraphEntries> {
  const query = datalinkBrowserQuery({
    graph_version: options.graphVersion,
    entry_type: options.entryType,
    query: options.query?.trim() || undefined,
    page: options.page,
    page_size: options.pageSize,
  });
  return requestJson<DataLinkGraphEntries>(
    `/datasources/${encodeURIComponent(datasourceId)}/datalink/entries${query}`,
  );
}

export function getDatasourceDatalinkSubgraph(
  datasourceId: string,
  rootNodeId: string,
  options: DataLinkSubgraphQuery = {},
): Promise<DataLinkSubgraph> {
  const query = datalinkBrowserQuery(
    {
      root_node_id: rootNodeId,
      graph_version: options.graphVersion,
      hops: options.hops,
    },
    options.edgeTypes,
  );
  return requestJson<DataLinkSubgraph>(
    `/datasources/${encodeURIComponent(datasourceId)}/datalink/subgraph${query}`,
  );
}

export interface DataLinkCatalogQuery {
  graphVersion: string;
  type?: DataLinkNodeType;
  query?: string;
  nodeId?: string;
  page?: number;
  pageSize?: number;
}

function catalogQuery(options: DataLinkCatalogQuery): string {
  return datalinkBrowserQuery({
    graph_version: options.graphVersion,
    type: options.type,
    query: options.query?.trim() || undefined,
    node_id: options.nodeId,
    page: options.page,
    page_size: options.pageSize,
  });
}

export function getDatalinkCatalog(datasourceId: string, options: DataLinkCatalogQuery, signal?: AbortSignal): Promise<DataLinkCatalog> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/catalog${catalogQuery(options)}`, { signal });
}

export function getDatalinkCatalogDetail(datasourceId: string, nodeId: string, options: DataLinkCatalogQuery, signal?: AbortSignal): Promise<DataLinkCatalogDetail> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/catalog/${encodeURIComponent(nodeId)}${catalogQuery(options)}`, { signal });
}

export function getDatalinkRelations(datasourceId: string, options: DataLinkCatalogQuery, signal?: AbortSignal): Promise<DataLinkRelations> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/relations${catalogQuery(options)}`, { signal });
}

export function previewDatalink(datasourceId: string, body: DataLinkPreviewRequest, signal?: AbortSignal): Promise<DataLinkPreview> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/preview`, {
    method: "POST", body: JSON.stringify(body), signal,
  });
}

export function getDatalinkDraft(datasourceId: string, signal?: AbortSignal): Promise<DataLinkDraft | null> {
  return requestJson<DataLinkDraft | null>(`/datasources/${encodeURIComponent(datasourceId)}/datalink/draft`, { signal });
}

export function saveDatalinkDraft(datasourceId: string, body: { base_graph_version: string; schema_revision: number; expected_draft_revision?: number | null; changes: DataLinkDraftChange[] }, signal?: AbortSignal): Promise<DataLinkDraft> {
  return requestJson<DataLinkDraft>(`/datasources/${encodeURIComponent(datasourceId)}/datalink/draft`, { method: "PUT", body: JSON.stringify(body), signal });
}

export function publishDatalinkDraft(datasourceId: string, body: { expected_head: string; expected_draft_revision: number; idempotency_key: string }, signal?: AbortSignal): Promise<DataLinkPublishResult> {
  return requestJson<DataLinkPublishResult>(`/datasources/${encodeURIComponent(datasourceId)}/datalink/publish`, { method: "POST", body: JSON.stringify(body), signal });
}

export function previewDatalinkDraft(datasourceId: string, body: DataLinkDraftPreviewRequest, signal?: AbortSignal): Promise<DataLinkDraftPreview> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/draft/preview`, { method: "POST", body: JSON.stringify(body), signal });
}

export function getDatalinkVersions(datasourceId: string, page = 1, signal?: AbortSignal): Promise<DataLinkVersions> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/versions?page=${page}&page_size=20`, { signal });
}
export function restoreDatalinkVersion(datasourceId: string, body: DataLinkRestoreRequest, signal?: AbortSignal): Promise<DataLinkVersionPublish> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/restore`, { method: "POST", body: JSON.stringify(body), signal });
}
export function getDatalinkVersionConflicts(datasourceId: string, graphVersion: string, signal?: AbortSignal): Promise<DataLinkRebuildConflicts> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/versions/${encodeURIComponent(graphVersion)}/conflicts`, { signal });
}
export function getDatalinkVersionDiff(datasourceId: string, graphVersion: string, signal?: AbortSignal): Promise<DataLinkVersionDiff> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/versions/${encodeURIComponent(graphVersion)}/diff`, { signal });
}
export function getDatalinkVersionCatalog(datasourceId: string, graphVersion: string, type: DataLinkNodeType, page = 1, signal?: AbortSignal): Promise<DataLinkCatalog> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/versions/${encodeURIComponent(graphVersion)}/catalog?node_type=${type}&page=${page}&page_size=100`, { signal });
}
export function resolveDatalinkCandidate(datasourceId: string, body: DataLinkResolveCandidateRequest, signal?: AbortSignal): Promise<DataLinkVersionPublish> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/resolve-candidate`, { method: "POST", body: JSON.stringify(body), signal });
}
export function createDatalinkValidation(datasourceId: string, body: DataLinkValidationRequest, signal?: AbortSignal): Promise<DataLinkValidation> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/validations`, { method: "POST", body: JSON.stringify(body), signal });
}
export function listDatalinkValidations(datasourceId: string, signal?: AbortSignal): Promise<DataLinkValidation[]> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/validations`, { signal });
}
export function getDatalinkValidation(datasourceId: string, validationId: string, signal?: AbortSignal): Promise<DataLinkValidation> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/validations/${encodeURIComponent(validationId)}`, { signal });
}
export function cancelDatalinkValidation(datasourceId: string, validationId: string, signal?: AbortSignal): Promise<DataLinkValidation> {
  return requestJson(`/datasources/${encodeURIComponent(datasourceId)}/datalink/validations/${encodeURIComponent(validationId)}/cancel`, { method: "POST", signal });
}
