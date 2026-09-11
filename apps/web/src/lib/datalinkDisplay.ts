import type {
  DataLinkBrowserNode,
  DataLinkCatalogItem,
  DataLinkCatalogMappedColumn,
  DataLinkCatalogMapping,
  DataLinkCatalogRelation,
  DataLinkEdgeType,
  DataLinkNodeType,
  DataLinkProvenance,
} from "@/api/types";

export const datalinkNodeLabels: Record<DataLinkNodeType, string> = {
  table: "表", column: "字段", concept: "业务属性", entity: "业务实体",
};

export const datalinkRelationLabels: Record<DataLinkEdgeType, string> = {
  contains: "包含字段", foreign_key: "数据库外键", joinable: "候选关联",
  semantic_synonym: "语义同义", represents: "字段映射", has_concept: "实体归属",
  distribution_similar: "分布相似", correlated: "数值相关",
};

export const datalinkRelationTypeOrder: DataLinkEdgeType[] = [
  "foreign_key",
  "joinable",
  "semantic_synonym",
  "distribution_similar",
  "correlated",
  "contains",
  "represents",
  "has_concept",
];

export const datalinkFieldToFieldRelationTypes: readonly DataLinkEdgeType[] = [
  "foreign_key",
  "joinable",
  "semantic_synonym",
  "distribution_similar",
  "correlated",
];

export const datalinkProvenanceLabels: Record<DataLinkProvenance, string> = {
  structural: "数据源结构",
  database_foreign_key: "数据库外键声明",
  inferred_candidate: "自动推断候选",
  semantic_mapping: "语义映射",
  manual: "人工修订",
  unknown: "历史来源未记录",
};

export const datalinkRelationProvenanceLabels: Record<DataLinkProvenance, string> = {
  ...datalinkProvenanceLabels,
  manual: "人工候选 · 未核验",
};

export interface CompactCatalogMapping {
  column: DataLinkBrowserNode;
  concept: DataLinkBrowserNode;
  entity: DataLinkBrowserNode | null;
  fieldConfidence: number | null;
  extraEntities: { name: string; confidence: number | null }[];
}

export function datalinkNodeName(node: DataLinkBrowserNode): string {
  return node.type === "column" && node.table ? `${node.table}.${node.name}` : node.name;
}

export function datalinkColumnRef(column: DataLinkCatalogMappedColumn | Pick<DataLinkBrowserNode, "table" | "name">): string {
  return column.table ? `${column.table}.${column.name}` : column.name;
}

export function datalinkConfidence(value: number | null): string {
  return value === null ? "未记录" : `${(value * 100).toFixed(1)}%`;
}

export function datalinkMappedSummary(item: DataLinkCatalogItem): string {
  const mapping = item.primary_mapping;
  if (!mapping) return "";
  return mapping.concept_description
    ? `${mapping.concept_name}：${mapping.concept_description}`
    : mapping.concept_name;
}

export function fieldRowConcept(item: DataLinkCatalogItem): string {
  return item.primary_mapping?.concept_name ?? "未映射";
}

export function fieldRowDescription(item: DataLinkCatalogItem): string {
  const mapped = item.primary_mapping?.concept_description?.trim();
  if (mapped) return mapped;
  if (item.primary_mapping) return "";
  return item.node.description?.trim() ?? "";
}

export function fieldRowEntity(item: DataLinkCatalogItem): string {
  return item.primary_mapping?.entity_name ?? "";
}

export function fieldHasManualNote(item: DataLinkCatalogItem): boolean {
  return Boolean(item.primary_mapping && item.node.description?.trim());
}

export function datalinkMappedColumnList(item: DataLinkCatalogItem): string {
  return (item.mapped_columns ?? []).map(datalinkColumnRef).join(" · ");
}

export const DATALINK_LIST_PAGE_SIZE = 20;
export const FIELD_CATALOG_FETCH_PAGE_SIZE = 100;

export interface CatalogTableGroup {
  table: string;
  items: DataLinkCatalogItem[];
}

export interface CatalogEntityGroup {
  key: string;
  entity: DataLinkCatalogItem | null;
  items: DataLinkCatalogItem[];
}

function mappedColumnKey(column: DataLinkCatalogMappedColumn): string {
  return `${column.table ?? ""}\0${column.name}`;
}

export function groupConceptsByOwningEntity(
  concepts: DataLinkCatalogItem[],
  entities: DataLinkCatalogItem[],
): CatalogEntityGroup[] {
  const entityColumns = new Map(
    entities.map((entity) => [
      entity.node.id,
      new Set((entity.mapped_columns ?? []).map(mappedColumnKey)),
    ]),
  );
  const grouped = new Map<string, DataLinkCatalogItem[]>();
  const unassigned: DataLinkCatalogItem[] = [];
  for (const concept of concepts) {
    const keys = (concept.mapped_columns ?? []).map(mappedColumnKey);
    const owners = entities.filter((entity) => {
      const columns = entityColumns.get(entity.node.id);
      return columns != null && keys.some((key) => columns.has(key));
    });
    if (owners.length === 0) {
      unassigned.push(concept);
      continue;
    }
    for (const owner of owners) {
      const current = grouped.get(owner.node.id);
      if (current) current.push(concept);
      else grouped.set(owner.node.id, [concept]);
    }
  }
  const named = entities
    .filter((entity) => grouped.has(entity.node.id))
    .sort((left, right) => left.node.name.localeCompare(right.node.name, "zh"))
    .map((entity) => ({
      key: entity.node.id,
      entity,
      items: [...(grouped.get(entity.node.id) ?? [])].sort((left, right) => (
        left.node.name.localeCompare(right.node.name, "zh")
      )),
    }));
  if (unassigned.length === 0) return named;
  named.push({
    key: "unassigned",
    entity: null,
    items: [...unassigned].sort((left, right) => left.node.name.localeCompare(right.node.name, "zh")),
  });
  return named;
}

export function groupCatalogByTable(items: DataLinkCatalogItem[]): CatalogTableGroup[] {
  const groups: CatalogTableGroup[] = [];
  const byTable = new Map<string, DataLinkCatalogItem[]>();
  for (const item of items) {
    const table = item.node.table?.trim() || "未归属表";
    let grouped = byTable.get(table);
    if (!grouped) {
      grouped = [];
      byTable.set(table, grouped);
      groups.push({ table, items: grouped });
    }
    grouped.push(item);
  }
  return groups;
}

export interface FieldRelationRow {
  key: string;
  table: string;
  relation: DataLinkCatalogRelation;
  local: DataLinkBrowserNode;
  other: DataLinkBrowserNode;
}

export interface FieldRelationTableGroup {
  table: string;
  items: FieldRelationRow[];
}

export function relationRowStatus(relation: DataLinkCatalogRelation): string {
  if (!relation.enabled) return "已禁用";
  if (relation.join_eligible) return "可用于 Join 候选";
  if (relation.provenance === "manual") return "人工候选 · 未核验";
  return "";
}

function isFieldEndpoint(node: DataLinkBrowserNode): boolean {
  return node.type === "column" && Boolean(node.table?.trim());
}

export function groupRelationsByTable(items: readonly DataLinkCatalogRelation[]): FieldRelationTableGroup[] {
  const allowed = new Set<DataLinkEdgeType>(datalinkFieldToFieldRelationTypes);
  const byTable = new Map<string, FieldRelationRow[]>();
  const groups: FieldRelationTableGroup[] = [];

  function addRow(table: string, relation: DataLinkCatalogRelation, local: DataLinkBrowserNode, other: DataLinkBrowserNode): void {
    const key = `${table}:${relation.id}`;
    let grouped = byTable.get(table);
    if (!grouped) {
      grouped = [];
      byTable.set(table, grouped);
      groups.push({ table, items: grouped });
    }
    if (grouped.some((row) => row.key === key)) return;
    grouped.push({ key, table, relation, local, other });
  }

  for (const item of items) {
    const sourceTable = item.source.table?.trim() ?? "";
    const targetTable = item.target.table?.trim() ?? "";
    if (!allowed.has(item.type) || !isFieldEndpoint(item.source) || !isFieldEndpoint(item.target) || !sourceTable || !targetTable) continue;
    addRow(sourceTable, item, item.source, item.target);
    addRow(targetTable, item, item.target, item.source);
  }

  groups.sort((left, right) => left.table.localeCompare(right.table, "zh"));
  for (const group of groups) {
    group.items.sort((left, right) => {
      const local = left.local.name.localeCompare(right.local.name, "zh");
      if (local !== 0) return local;
      const type = datalinkRelationTypeOrder.indexOf(left.relation.type) - datalinkRelationTypeOrder.indexOf(right.relation.type);
      if (type !== 0) return type;
      return datalinkNodeName(left.other).localeCompare(datalinkNodeName(right.other), "zh");
    });
  }
  return groups;
}

export function uniqueFieldRelationCount(groups: readonly FieldRelationTableGroup[]): number {
  const ids = new Set<string>();
  for (const group of groups) {
    for (const row of group.items) ids.add(row.relation.id);
  }
  return ids.size;
}

export function indexCatalogColumns(items: readonly DataLinkCatalogItem[]): Map<string, DataLinkCatalogItem> {
  const index = new Map<string, DataLinkCatalogItem>();
  for (const item of items) {
    if (item.node.type !== "column") continue;
    index.set(item.node.id, item);
    index.set(`${item.node.table?.trim() ?? ""}\0${item.node.name}`, item);
  }
  return index;
}

export function lookupCatalogColumn(
  index: Map<string, DataLinkCatalogItem>,
  node: DataLinkBrowserNode,
): DataLinkCatalogItem | undefined {
  return index.get(node.id) ?? index.get(`${node.table?.trim() ?? ""}\0${node.name}`);
}

export function fieldRelationConcept(
  index: Map<string, DataLinkCatalogItem>,
  node: DataLinkBrowserNode,
): string {
  const item = lookupCatalogColumn(index, node);
  return item ? fieldRowConcept(item) : "未映射";
}

export function fieldRelationEntity(
  index: Map<string, DataLinkCatalogItem>,
  node: DataLinkBrowserNode,
): string {
  const item = lookupCatalogColumn(index, node);
  return item ? fieldRowEntity(item) : "";
}

export function paginateTableGroups<T extends { items: readonly unknown[] }>(
  groups: T[],
  rowBudget = DATALINK_LIST_PAGE_SIZE,
): T[][] {
  const pages: T[][] = [];
  let current: T[] = [];
  let count = 0;
  for (const group of groups) {
    if (current.length > 0 && count + group.items.length > rowBudget) {
      pages.push(current);
      current = [];
      count = 0;
    }
    current.push(group);
    count += group.items.length;
  }
  if (current.length) pages.push(current);
  return pages;
}

function mappingRank(mapping: DataLinkCatalogMapping): [number, number, number, number, number, string, string] {
  const field = mapping.field_to_concept_confidence;
  const entity = mapping.entity_to_concept_confidence;
  return [
    field == null ? 1 : 0,
    -(field ?? 0),
    mapping.entity ? 0 : 1,
    entity == null ? 1 : 0,
    -(entity ?? 0),
    mapping.concept.id,
    mapping.entity?.id ?? "",
  ];
}

function rankLess(left: DataLinkCatalogMapping, right: DataLinkCatalogMapping): number {
  const a = mappingRank(left);
  const b = mappingRank(right);
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] < b[index]) return -1;
    if (a[index] > b[index]) return 1;
  }
  return 0;
}

export function compactCatalogMappings(mappings: DataLinkCatalogMapping[]): CompactCatalogMapping[] {
  const groups = new Map<string, DataLinkCatalogMapping[]>();
  for (const mapping of mappings) {
    if (mapping.enabled === false) continue;
    const key = `${mapping.column.id}\0${mapping.concept.id}`;
    const current = groups.get(key);
    if (current) current.push(mapping);
    else groups.set(key, [mapping]);
  }
  return [...groups.values()].map((group) => {
    const sorted = [...group].sort(rankLess);
    const primary = sorted[0];
    const seen = new Set<string>(primary.entity ? [primary.entity.id] : []);
    const extraEntities: CompactCatalogMapping["extraEntities"] = [];
    for (const item of sorted) {
      if (!item.entity || seen.has(item.entity.id)) continue;
      seen.add(item.entity.id);
      extraEntities.push({ name: item.entity.name, confidence: item.entity_to_concept_confidence ?? null });
    }
    extraEntities.sort((left, right) => (right.confidence ?? -1) - (left.confidence ?? -1) || left.name.localeCompare(right.name, "zh"));
    return {
      column: primary.column,
      concept: primary.concept,
      entity: primary.entity,
      fieldConfidence: primary.field_to_concept_confidence,
      extraEntities,
    };
  });
}
