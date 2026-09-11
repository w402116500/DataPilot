import type {
  DataLinkBrowserNode,
  DataLinkEdge,
  DataLinkEdgeType,
  DataLinkNodeType,
  DataLinkSubgraph,
} from "@/api/types";
import { datalinkColumnRef } from "@/lib/datalinkDisplay";

export const SEMANTIC_NODE_TYPES = ["entity", "concept"] as const satisfies readonly DataLinkNodeType[];
export const SEMANTIC_CANVAS_EDGE_TYPES = ["has_concept", "semantic_synonym"] as const satisfies readonly DataLinkEdgeType[];
export const SEMANTIC_FIELD_EDGE_TYPE = "represents" satisfies DataLinkEdgeType;

const LAYOUT_WIDTH = 1000;
const LAYOUT_HEIGHT = 720;
export const LAYOUT_CENTER = { x: LAYOUT_WIDTH / 2, y: LAYOUT_HEIGHT / 2 };

export interface SemanticMapProjection {
  nodes: DataLinkBrowserNode[];
  edges: DataLinkEdge[];
}

export interface SemanticMapPosition {
  x: number;
  y: number;
}

export type SemanticLabelSide = "top" | "right" | "bottom" | "left";

function isSemanticNode(node: DataLinkBrowserNode): boolean {
  return node.type === "entity" || node.type === "concept";
}

function isSemanticCanvasEdge(edge: DataLinkEdge): boolean {
  return edge.type === "has_concept" || edge.type === "semantic_synonym";
}

export function semanticSubgraphEdgeTypes(filter: readonly DataLinkEdgeType[]): DataLinkEdgeType[] {
  const visible = filter.length === 0
    ? [...SEMANTIC_CANVAS_EDGE_TYPES]
    : SEMANTIC_CANVAS_EDGE_TYPES.filter((type) => filter.includes(type));
  return [...visible, SEMANTIC_FIELD_EDGE_TYPE];
}

export function projectSemanticMap(subgraph: DataLinkSubgraph): SemanticMapProjection {
  const nodes = subgraph.nodes.filter(isSemanticNode);
  const ids = new Set(nodes.map((node) => node.id));
  const edges = subgraph.edges.filter(
    (edge) => isSemanticCanvasEdge(edge) && ids.has(edge.source) && ids.has(edge.target),
  );
  return { nodes, edges };
}

function byName(left: DataLinkBrowserNode, right: DataLinkBrowserNode): number {
  return left.name.localeCompare(right.name, "zh") || left.id.localeCompare(right.id);
}

function polar(
  center: SemanticMapPosition,
  radius: number,
  index: number,
  count: number,
): SemanticMapPosition {
  const n = Math.max(count, 1);
  // 1–2 nodes on the axes make View stretch a degenerate bbox into a long line.
  const angle = -Math.PI / 2 + (n < 3 ? Math.PI / 5 : 0) + (2 * Math.PI * index) / n;
  return {
    x: center.x + radius * Math.cos(angle),
    y: center.y + radius * Math.sin(angle),
  };
}

export function layoutSemanticMap(
  nodes: readonly DataLinkBrowserNode[],
  edges: readonly DataLinkEdge[],
): Map<string, SemanticMapPosition> {
  const positions = new Map<string, SemanticMapPosition>();
  const entities = nodes.filter((node) => node.type === "entity").slice().sort(byName);
  const concepts = nodes.filter((node) => node.type === "concept").slice().sort(byName);
  const ownedByEntity = new Map<string, string[]>();
  const ownersOfConcept = new Map<string, string[]>();

  for (const edge of edges) {
    if (edge.type !== "has_concept") continue;
    const owned = ownedByEntity.get(edge.source) ?? [];
    owned.push(edge.target);
    ownedByEntity.set(edge.source, owned);
    const owners = ownersOfConcept.get(edge.target) ?? [];
    owners.push(edge.source);
    ownersOfConcept.set(edge.target, owners);
  }

  if (entities.length === 0) {
    concepts.forEach((node, index) => {
      positions.set(node.id, polar(LAYOUT_CENTER, 170, index, concepts.length));
    });
    return positions;
  }

  if (entities.length === 1) {
    const entity = entities[0];
    positions.set(entity.id, LAYOUT_CENTER);
    const owned = [...new Set(ownedByEntity.get(entity.id) ?? [])];
    const radius = owned.length === 1 ? 140 : 200;
    owned.forEach((id, index) => {
      positions.set(id, polar(LAYOUT_CENTER, radius, index, owned.length));
    });
    const orphans = concepts.filter((node) => !positions.has(node.id));
    orphans.forEach((node, index) => {
      positions.set(node.id, polar(LAYOUT_CENTER, 300, index, orphans.length));
    });
    return positions;
  }

  entities.forEach((entity, index) => {
    positions.set(entity.id, polar(LAYOUT_CENTER, 168, index, entities.length));
  });

  for (const entity of entities) {
    const origin = positions.get(entity.id);
    if (!origin) continue;
    const owned = [...new Set(ownedByEntity.get(entity.id) ?? [])].filter((id) => {
      const owners = ownersOfConcept.get(id) ?? [];
      return owners.length <= 1 && !positions.has(id);
    });
    owned.forEach((id, index) => {
      positions.set(id, polar(origin, 96, index, owned.length));
    });
  }

  for (const concept of concepts) {
    if (positions.has(concept.id)) continue;
    const owners = (ownersOfConcept.get(concept.id) ?? [])
      .map((id) => positions.get(id))
      .filter((position): position is SemanticMapPosition => position !== undefined);
    if (owners.length === 0) continue;
    positions.set(concept.id, {
      x: owners.reduce((sum, position) => sum + position.x, 0) / owners.length,
      y: owners.reduce((sum, position) => sum + position.y, 0) / owners.length,
    });
  }

  const unplaced = concepts.filter((node) => !positions.has(node.id));
  unplaced.forEach((node, index) => {
    positions.set(node.id, polar(LAYOUT_CENTER, 280, index, unplaced.length));
  });
  return positions;
}

export function outwardLabelPosition(
  point: SemanticMapPosition,
  origin: SemanticMapPosition = LAYOUT_CENTER,
): SemanticLabelSide {
  const dx = point.x - origin.x;
  const dy = point.y - origin.y;
  if (Math.abs(dx) > Math.abs(dy)) return dx >= 0 ? "right" : "left";
  return dy >= 0 ? "bottom" : "top";
}

export function conceptLabelOrigin(
  conceptId: string,
  edges: readonly DataLinkEdge[],
  positions: ReadonlyMap<string, SemanticMapPosition>,
): SemanticMapPosition {
  const owners = edges
    .filter((edge) => edge.type === "has_concept" && edge.target === conceptId)
    .map((edge) => positions.get(edge.source))
    .filter((position): position is SemanticMapPosition => position !== undefined);
  if (owners.length === 0) return LAYOUT_CENTER;
  return {
    x: owners.reduce((sum, position) => sum + position.x, 0) / owners.length,
    y: owners.reduce((sum, position) => sum + position.y, 0) / owners.length,
  };
}

export function relatedConcepts(subgraph: DataLinkSubgraph, entityId: string): DataLinkBrowserNode[] {
  const conceptIds = new Set(
    subgraph.edges
      .filter((edge) => edge.type === "has_concept" && edge.source === entityId)
      .map((edge) => edge.target),
  );
  return subgraph.nodes.filter((node) => node.type === "concept" && conceptIds.has(node.id));
}

export function relatedConceptNames(subgraph: DataLinkSubgraph, entityId: string): string[] {
  return relatedConcepts(subgraph, entityId).map((node) => node.name);
}

export function owningEntities(subgraph: DataLinkSubgraph, conceptId: string): DataLinkBrowserNode[] {
  const entityIds = new Set(
    subgraph.edges
      .filter((edge) => edge.type === "has_concept" && edge.target === conceptId)
      .map((edge) => edge.source),
  );
  return subgraph.nodes.filter((node) => node.type === "entity" && entityIds.has(node.id));
}

export function owningEntityNames(subgraph: DataLinkSubgraph, conceptId: string): string[] {
  return owningEntities(subgraph, conceptId).map((node) => node.name);
}

export function mappedFieldLabels(subgraph: DataLinkSubgraph, node: DataLinkBrowserNode): string[] {
  const conceptIds = node.type === "concept"
    ? new Set([node.id])
    : new Set(
      subgraph.edges
        .filter((edge) => edge.type === "has_concept" && edge.source === node.id)
        .map((edge) => edge.target),
    );
  const columnIds = new Set(
    subgraph.edges
      .filter((edge) => edge.type === SEMANTIC_FIELD_EDGE_TYPE && conceptIds.has(edge.target))
      .map((edge) => edge.source),
  );
  return subgraph.nodes
    .filter((candidate) => candidate.type === "column" && columnIds.has(candidate.id))
    .map((candidate) => datalinkColumnRef(candidate));
}
