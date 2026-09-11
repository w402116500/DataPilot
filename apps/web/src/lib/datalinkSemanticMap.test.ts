import { describe, expect, it } from "vitest";

import type { DataLinkBrowserNode, DataLinkEdge, DataLinkSubgraph } from "@/api/types";
import {
  layoutSemanticMap,
  mappedFieldLabels,
  outwardLabelPosition,
  owningEntities,
  owningEntityNames,
  projectSemanticMap,
  relatedConcepts,
  relatedConceptNames,
  semanticSubgraphEdgeTypes,
} from "./datalinkSemanticMap";

function node(
  id: string,
  type: DataLinkBrowserNode["type"],
  name = id,
  table: string | null = null,
): DataLinkBrowserNode {
  return {
    id,
    type,
    name,
    table,
    semantic_type: null,
    description: null,
    aliases: [],
    profile: null,
  };
}

function edge(source: string, target: string, type: DataLinkEdge["type"]): DataLinkEdge {
  return { source, target, type, confidence: null, evidence: null };
}

const subgraph: DataLinkSubgraph = {
  datasource_id: "ds_1",
  graph_version: "graph_1",
  root_node_id: "entity:customer",
  total_node_count: 6,
  total_edge_count: 5,
  nodes: [
    node("table:customers", "table", "customers", "customers"),
    node("column:customers.email", "column", "email", "customers"),
    node("concept:email", "concept", "邮箱"),
    node("concept:name", "concept", "客户姓名"),
    node("entity:customer", "entity", "客户"),
  ],
  edges: [
    edge("table:customers", "column:customers.email", "contains"),
    edge("column:customers.email", "concept:email", "represents"),
    edge("entity:customer", "concept:email", "has_concept"),
    edge("entity:customer", "concept:name", "has_concept"),
    edge("concept:email", "concept:name", "semantic_synonym"),
    edge("column:customers.email", "column:orders.email", "joinable"),
  ],
  is_truncated: false,
  warnings: [],
};

describe("datalinkSemanticMap", () => {
  it("subgraph 请求始终带字段映射边，供详情列出物理字段", () => {
    expect(semanticSubgraphEdgeTypes([])).toEqual(["has_concept", "semantic_synonym", "represents"]);
    expect(semanticSubgraphEdgeTypes(["semantic_synonym"])).toEqual(["semantic_synonym", "represents"]);
  });

  it("画布只保留实体、概念和语义边", () => {
    expect(projectSemanticMap(subgraph)).toEqual({
      nodes: [
        node("concept:email", "concept", "邮箱"),
        node("concept:name", "concept", "客户姓名"),
        node("entity:customer", "entity", "客户"),
      ],
      edges: [
        edge("entity:customer", "concept:email", "has_concept"),
        edge("entity:customer", "concept:name", "has_concept"),
        edge("concept:email", "concept:name", "semantic_synonym"),
      ],
    });
  });

  it("单个实体放在中心，所属概念围成一圈", () => {
    const projected = projectSemanticMap(subgraph);
    const positions = layoutSemanticMap(projected.nodes, projected.edges);
    const entity = positions.get("entity:customer")!;
    const email = positions.get("concept:email")!;
    const name = positions.get("concept:name")!;
    expect(entity).toEqual({ x: 500, y: 360 });
    expect(email).not.toEqual(name);
    expect(Math.hypot(email.x - entity.x, email.y - entity.y)).toBeCloseTo(200);
    expect(Math.hypot(name.x - entity.x, name.y - entity.y)).toBeCloseTo(200);
    expect(outwardLabelPosition(email, entity)).toMatch(/top|right|bottom|left/);
    expect(outwardLabelPosition(email, entity)).not.toEqual(outwardLabelPosition(name, entity));
  });

  it("单实体单概念错开，避免共线把画布拉扁", () => {
    const entity = node("entity:event", "entity", "事件");
    const concept = node("concept:event-id", "concept", "事件唯一标识");
    const positions = layoutSemanticMap(
      [entity, concept],
      [edge("entity:event", "concept:event-id", "has_concept")],
    );
    const hub = positions.get(entity.id)!;
    const spoke = positions.get(concept.id)!;
    expect(hub).toEqual({ x: 500, y: 360 });
    expect(Math.abs(spoke.x - hub.x)).toBeGreaterThan(40);
    expect(Math.abs(spoke.y - hub.y)).toBeGreaterThan(40);
    expect(Math.hypot(spoke.x - hub.x, spoke.y - hub.y)).toBeCloseTo(140);
    expect(outwardLabelPosition(spoke, hub)).toBe("top");
  });

  it("详情从已有边读取概念、实体和映射字段，不从表名重算归属", () => {
    expect(relatedConcepts(subgraph, "entity:customer").map((node) => node.id)).toEqual([
      "concept:email",
      "concept:name",
    ]);
    expect(relatedConceptNames(subgraph, "entity:customer")).toEqual(["邮箱", "客户姓名"]);
    expect(owningEntities(subgraph, "concept:email").map((node) => node.id)).toEqual(["entity:customer"]);
    expect(owningEntityNames(subgraph, "concept:email")).toEqual(["客户"]);
    expect(mappedFieldLabels(subgraph, node("concept:email", "concept", "邮箱"))).toEqual([
      "customers.email",
    ]);
    expect(mappedFieldLabels(subgraph, node("entity:customer", "entity", "客户"))).toEqual([
      "customers.email",
    ]);
  });
});
