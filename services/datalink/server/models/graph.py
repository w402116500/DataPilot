"""DataLink 图谱结构的内部模型，不作为 REST 或 MCP 契约直接暴露。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from contracts.datalink import (
    DataLinkEdgeEvidenceRead,
    DataLinkEdgeType,
    DataLinkNodeType,
    DataLinkProvenance,
)

type GraphPropertyValue = str | int | float | bool | None


def graph_provenance(value: GraphNode | GraphEdge) -> DataLinkProvenance:
    """Preserve recorded provenance; unrecorded legacy mappings stay unknown."""
    recorded = value.properties.get("provenance")
    if recorded in {
        "manual",
        "structural",
        "database_foreign_key",
        "inferred_candidate",
        "semantic_mapping",
        "unknown",
    }:
        return recorded
    if isinstance(value, GraphNode):
        return (
            "structural"
            if value.type in {DataLinkNodeType.TABLE, DataLinkNodeType.COLUMN}
            else "unknown"
        )
    if value.type == DataLinkEdgeType.FOREIGN_KEY:
        return "database_foreign_key"
    if value.type == DataLinkEdgeType.JOINABLE:
        return "inferred_candidate"
    return "unknown"


def edge_enabled(edge: GraphEdge) -> bool:
    """Missing flags preserve legacy edges; manual candidates need explicit enablement."""
    needs_confirmation = (
        graph_provenance(edge) == "manual" and edge.type == DataLinkEdgeType.JOINABLE
    )
    return edge.properties.get("enabled", not needs_confirmation) is True


@dataclass(frozen=True)
class GraphNode:
    """一条已从受控数据源抽出的图谱节点。"""

    id: str
    type: DataLinkNodeType
    name: str
    table_name: str | None = None
    semantic_type: str | None = None
    description: str | None = None
    aliases: tuple[str, ...] = ()
    properties: Mapping[str, GraphPropertyValue] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    """一条有明确类型和证据的内部图谱关系。"""

    id: str
    source_id: str
    target_id: str
    type: DataLinkEdgeType
    confidence: float | None
    evidence: DataLinkEdgeEvidenceRead | None = None
    properties: Mapping[str, GraphPropertyValue] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEmbedding:
    """供检索使用的可选节点向量，绝不作为 REST 或 MCP 输出。"""

    node_id: str
    embedding_model: str
    vector: Sequence[float]
    searchable_text: str


@dataclass(frozen=True)
class GraphPendingEdge:
    """引用尚未出现节点的候选边，只供后续重建复核，不能参与 Join。"""

    id: str
    source_id: str
    target_ref: str
    type: DataLinkEdgeType
    confidence: float
    missing_endpoints: tuple[str, ...]
    properties: Mapping[str, GraphPropertyValue] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphStructure:
    """Extract 阶段产生的节点和关系集合，供画像和推断继续加工。"""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    pending_edges: tuple[GraphPendingEdge, ...] = ()

    @property
    def columns(self) -> tuple[GraphNode, ...]:
        """返回字段节点，避免推断器重复了解节点存储细节。"""

        return tuple(node for node in self.nodes if node.type == DataLinkNodeType.COLUMN)
