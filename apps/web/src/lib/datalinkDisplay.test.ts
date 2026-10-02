import { describe, expect, it } from "vitest";
import type { DataLinkCatalogItem, DataLinkCatalogRelation, DataLinkEdgeType } from "@/api/types";
import {
  datalinkProvenanceLabels,
  datalinkRelationProvenanceLabels,
  fieldRelationConcept,
  fieldRelationEntity,
  groupCatalogByTable,
  groupConceptsByOwningEntity,
  groupRelationsByTable,
  indexCatalogColumns,
  paginateTableGroups,
  relationRowStatus,
  uniqueFieldRelationCount,
} from "./datalinkDisplay";

function column(table: string, name: string): DataLinkCatalogItem {
  return {
    node: { id: `${table}.${name}`, name, type: "column", table, description: null, aliases: [], semantic_type: null, profile: null },
    provenance: "structural", mapping_count: 0, relation_count: 0, primary_mapping: null,
  };
}

function names(groups: ReturnType<typeof groupCatalogByTable>): string[] {
  return groups.flatMap((group) => group.items.map((item) => `${group.table}.${item.node.name}`));
}

describe("datalinkProvenanceLabels", () => {
  it("labels semantic mapping without treating it as unrecorded or a model name", () => {
    expect(datalinkProvenanceLabels.semantic_mapping).toBe("语义映射");
    expect(datalinkRelationProvenanceLabels.semantic_mapping).toBe("语义映射");
    expect(datalinkProvenanceLabels.unknown).toBe("历史来源未记录");
    expect(datalinkProvenanceLabels.manual).toBe("人工修订");
    expect(datalinkRelationProvenanceLabels.manual).toBe("人工候选 · 未核验");
  });
});

function concept(id: string, name: string, columns: { table: string; name: string }[]): DataLinkCatalogItem {
  return {
    node: { id, name, type: "concept", table: null, description: null, aliases: [], semantic_type: null, profile: null },
    provenance: "semantic_mapping",
    mapping_count: columns.length,
    relation_count: 1,
    mapped_columns: columns,
  };
}

function entity(id: string, name: string, columns: { table: string; name: string }[]): DataLinkCatalogItem {
  return {
    node: { id, name, type: "entity", table: null, description: null, aliases: [], semantic_type: null, profile: null },
    provenance: "semantic_mapping",
    mapping_count: columns.length,
    relation_count: 1,
    mapped_columns: columns,
  };
}

describe("groupConceptsByOwningEntity", () => {
  it("groups concepts by overlapping mapped columns and repeats shared concepts", () => {
    const event = entity("entity:event", "事件", [
      { table: "events", name: "event_id" },
      { table: "events", name: "event_ts" },
    ]);
    const product = entity("entity:product", "产品", [
      { table: "products", name: "product_name" },
      { table: "products", name: "product_id" },
      { table: "products", name: "unit_price" },
    ]);
    const customer = entity("entity:customer", "客户", [{ table: "customers", name: "customer_id" }]);
    const order = entity("entity:order", "订单", [{ table: "orders", name: "customer_id" }]);
    const groups = groupConceptsByOwningEntity(
      [
        concept("concept:price", "单价", [{ table: "products", name: "unit_price" }]),
        concept("concept:event-id", "事件唯一标识", [{ table: "events", name: "event_id" }]),
        concept("concept:product-name", "产品名称", [{ table: "products", name: "product_name" }]),
        concept("concept:customer-id", "客户标识", [
          { table: "customers", name: "customer_id" },
          { table: "orders", name: "customer_id" },
        ]),
        concept("concept:orphan", "孤立概念", []),
      ],
      [event, product, customer, order],
    );
    expect(groups.map((group) => group.entity?.node.name ?? "未归属")).toEqual(
      expect.arrayContaining(["产品", "客户", "事件", "订单", "未归属"]),
    );
    expect(groups).toHaveLength(5);
    expect(groups.at(-1)?.key).toBe("unassigned");
    const byOwner = Object.fromEntries(
      groups.map((group) => [group.entity?.node.name ?? "未归属", group.items.map((item) => item.node.name)]),
    );
    expect(byOwner["产品"]).toEqual(["产品名称", "单价"]);
    expect(byOwner["客户"]).toEqual(["客户标识"]);
    expect(byOwner["事件"]).toEqual(["事件唯一标识"]);
    expect(byOwner["订单"]).toEqual(["客户标识"]);
    expect(byOwner["未归属"]).toEqual(["孤立概念"]);
  });
});

describe("paginateTableGroups", () => {
  it("keeps a table together even when it exceeds the row budget", () => {
    const groups = groupCatalogByTable(Array.from({ length: 25 }, (_, index) => column("products", `c${index}`)));
    const pages = paginateTableGroups(groups, 20);
    expect(pages).toHaveLength(1);
    expect(pages[0]).toHaveLength(1);
    expect(pages[0][0].table).toBe("products");
    expect(pages[0][0].items).toHaveLength(25);
  });

  it("does not split the last table across pages", () => {
    const items = [
      ...["channel", "country", "customer_id", "signup_date"].map((name) => column("customers", name)),
      ...["customer_id", "event_id", "event_ts", "event_type", "session_id"].map((name) => column("events", name)),
      ...["order_id", "order_item_id", "product_id", "quantity", "unit_price"].map((name) => column("order_items", name)),
      ...["customer_id", "order_id", "order_ts", "status"].map((name) => column("orders", name)),
      ...["category", "product_id", "product_name", "unit_cost", "unit_price"].map((name) => column("products", name)),
    ];
    const pages = paginateTableGroups(groupCatalogByTable(items), 20);
    expect(pages).toHaveLength(2);
    expect(pages[0].map((group) => group.table)).toEqual(["customers", "events", "order_items", "orders"]);
    expect(names(pages[0])).toHaveLength(18);
    expect(pages[1].map((group) => group.table)).toEqual(["products"]);
    expect(names(pages[1])).toEqual([
      "products.category",
      "products.product_id",
      "products.product_name",
      "products.unit_cost",
      "products.unit_price",
    ]);
  });

  it("fills a page with whole tables until the next table would overflow", () => {
    const items = [
      ...Array.from({ length: 8 }, (_, index) => column("a", `a${index}`)),
      ...Array.from({ length: 8 }, (_, index) => column("b", `b${index}`)),
      ...Array.from({ length: 8 }, (_, index) => column("c", `c${index}`)),
    ];
    const pages = paginateTableGroups(groupCatalogByTable(items), 20);
    expect(pages.map((page) => page.map((group) => group.table))).toEqual([["a", "b"], ["c"]]);
  });
});

function relationNode(name: string, type: "column" | "concept" = "column", table: string | null = type === "column" ? "customers" : null) {
  return {
    id: table ? `${table}.${name}` : `concept:${name}`,
    name,
    type,
    table,
    description: null,
    aliases: [],
    semantic_type: null,
    profile: null,
  };
}

function relation(
  id: string,
  type: DataLinkEdgeType,
  source: ReturnType<typeof relationNode>,
  target: ReturnType<typeof relationNode>,
  extras: Partial<DataLinkCatalogRelation> = {},
): DataLinkCatalogRelation {
  return {
    id,
    enabled: true,
    join_eligible: false,
    type,
    confidence: null,
    provenance: "semantic_mapping",
    evidence: null,
    source,
    target,
    ...extras,
  };
}

describe("groupRelationsByTable", () => {
  it("fans a directed edge into both tables and skips mapping and contains", () => {
    const groups = groupRelationsByTable([
      relation("fk", "foreign_key", relationNode("customer_id", "column", "orders"), relationNode("id", "column", "customers")),
      relation("r", "represents", relationNode("channel"), relationNode("获客渠道", "concept")),
      relation("h", "has_concept", relationNode("客户", "concept"), relationNode("客户标识", "concept")),
      relation("c", "contains", relationNode("customers", "column", "customers"), relationNode("customer_id")),
    ]);
    expect(groups.map((group) => group.table)).toEqual(["customers", "orders"]);
    expect(groups[0]?.items).toHaveLength(1);
    expect(groups[0]?.items[0]?.local.name).toBe("id");
    expect(groups[0]?.items[0]?.other.name).toBe("customer_id");
    expect(groups[1]?.items[0]?.local.name).toBe("customer_id");
    expect(groups[1]?.items[0]?.other.name).toBe("id");
    expect(uniqueFieldRelationCount(groups)).toBe(1);
  });

  it("keeps a same-table relation once", () => {
    const groups = groupRelationsByTable([
      relation("s", "semantic_synonym", relationNode("channel"), relationNode("country")),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.items).toHaveLength(1);
    expect(groups[0]?.items[0]?.local.name).toBe("channel");
    expect(groups[0]?.items[0]?.other.name).toBe("country");
  });
});

describe("fieldRelationConcept", () => {
  it("reads catalog primary_mapping by node id or table and name", () => {
    const index = indexCatalogColumns([
      {
        node: {
          id: "customers.customer_id", name: "customer_id", type: "column", table: "customers",
          description: null, aliases: [], semantic_type: null, profile: null,
        },
        provenance: "structural",
        mapping_count: 1,
        relation_count: 1,
        primary_mapping: {
          concept_name: "客户标识", concept_description: "下单客户", entity_name: "客户",
          field_to_concept_confidence: 0.9, provenance: "unknown",
        },
      },
    ]);
    expect(fieldRelationConcept(index, relationNode("customer_id"))).toBe("客户标识");
    expect(fieldRelationConcept(index, { ...relationNode("customer_id"), id: "col:customer_id" })).toBe("客户标识");
    expect(fieldRelationConcept(index, relationNode("notes", "column", "orders"))).toBe("未映射");
    expect(fieldRelationEntity(index, relationNode("customer_id"))).toBe("客户");
    expect(fieldRelationEntity(index, { ...relationNode("customer_id"), id: "col:customer_id" })).toBe("客户");
    expect(fieldRelationEntity(index, relationNode("notes", "column", "orders"))).toBe("");
  });
});

describe("relationRowStatus", () => {
  const base = relation("x", "represents", relationNode("channel"), relationNode("获客渠道", "concept"));

  it("hides default semantic purpose copy", () => {
    expect(relationRowStatus(base)).toBe("");
  });

  it("labels join, disabled, and unverified manual candidates", () => {
    expect(relationRowStatus({ ...base, join_eligible: true })).toBe("可用于 Join 候选");
    expect(relationRowStatus({ ...base, enabled: false, join_eligible: true })).toBe("已禁用");
    expect(relationRowStatus({ ...base, provenance: "manual" })).toBe("人工候选 · 未核验");
  });
});
