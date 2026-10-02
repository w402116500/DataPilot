"""将全部字段严格映射为 Concept、Entity 和可追溯语义关系。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic

from contracts.datalink import DataLinkEdgeEvidenceRead, DataLinkEdgeType, DataLinkNodeType

from server.extractor.tabular import make_graph_id
from server.mapper.client import (
    ModelConfigurationError,
    ModelRequestTimeoutError,
    ModelResponseError,
    ModelResponseFormatError,
    ModelUpstreamError,
    SemanticModelClient,
)
from server.mapper.merger import SemanticMerger
from server.mapper.schemas import MappingColumnInput, SemanticMappingResponse
from server.models.graph import GraphEdge, GraphNode
from server.models.profile import ColumnProfile

logger = logging.getLogger(__name__)

# 当前语义模型在 120 秒上限内无法稳定完成 15 个字段的严格输出；五列批次已通过真实 SQLite
# 验收。保持固定的小批量，避免放宽超时或接受不完整的结构化响应。
_MAPPING_BATCH_SIZE = 5
_MERGE_BATCH_INTERVAL = 10


class SemanticMappingValidationError(ValueError):
    """模型引用越界或结构不成立时，拒绝该批语义节点。"""


@dataclass(frozen=True)
class SemanticMappingResult:
    """Map 和跨批同义整理结束后的语义节点与关系。"""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


class SemanticMapper:
    """按固定批次调用模型，字段语义映射失败必须中止当前 Build。"""

    def __init__(
        self,
        client: SemanticModelClient,
        *,
        batch_size: int = _MAPPING_BATCH_SIZE,
        merge_batch_interval: int = _MERGE_BATCH_INTERVAL,
    ) -> None:
        if batch_size < 1 or merge_batch_interval < 1:
            raise ValueError("Mapping batch settings must be positive")
        self.client = client
        self.batch_size = batch_size
        self.merge_batch_interval = merge_batch_interval
        self.merger = SemanticMerger(client)

    def map_columns(
        self,
        datasource_id: str,
        columns: Sequence[GraphNode],
        profiles: Sequence[ColumnProfile],
    ) -> SemanticMappingResult:
        """依原始字段顺序映射全部字段，失败交给 Build 所有者记录。"""

        profile_by_column = {profile.column_id: profile for profile in profiles}
        mapping_columns = tuple(
            column for column in columns if column.type == DataLinkNodeType.COLUMN
        )
        completed_nodes: tuple[GraphNode, ...] = ()
        completed_edges: tuple[GraphEdge, ...] = ()
        pending_nodes: list[GraphNode] = []
        pending_edges: list[GraphEdge] = []
        total_batches = (len(mapping_columns) + self.batch_size - 1) // self.batch_size

        for batch_index, start in enumerate(
            range(0, len(mapping_columns), self.batch_size), start=1
        ):
            batch_columns = mapping_columns[start : start + self.batch_size]
            inputs = tuple(
                MappingColumnInput(
                    column_id=column.id,
                    column_name=column.name,
                    table_name=column.table_name or "",
                    profile=profile_by_column[column.id].to_read(),
                )
                for column in batch_columns
            )
            logger.info(
                "DataLink semantic map batch started",
                extra={
                    "datasource_id": datasource_id,
                    "batch_index": batch_index,
                    "batch_count": total_batches,
                    "column_count": len(inputs),
                },
            )
            started_at = monotonic()
            try:
                response = self.client.map_batch(inputs)
                batch_nodes, batch_edges = self._build_batch(
                    datasource_id, batch_index, inputs, response
                )
            except Exception as exc:
                logger.warning(
                    "DataLink semantic map batch failed: batch=%d/%d elapsed_ms=%d failure=%s",
                    batch_index,
                    total_batches,
                    round((monotonic() - started_at) * 1000),
                    _mapping_failure_kind(exc),
                    extra={
                        "datasource_id": datasource_id,
                        "batch_index": batch_index,
                        "batch_count": total_batches,
                        "elapsed_ms": round((monotonic() - started_at) * 1000),
                        "failure_kind": _mapping_failure_kind(exc),
                    },
                )
                raise
            logger.info(
                "DataLink semantic map batch completed: batch=%d/%d elapsed_ms=%d",
                batch_index,
                total_batches,
                round((monotonic() - started_at) * 1000),
                extra={
                    "datasource_id": datasource_id,
                    "batch_index": batch_index,
                    "batch_count": total_batches,
                    "elapsed_ms": round((monotonic() - started_at) * 1000),
                },
            )
            pending_nodes.extend(batch_nodes)
            pending_edges.extend(batch_edges)

            if batch_index % self.merge_batch_interval == 0 or batch_index == total_batches:
                merged = self.merger.merge(
                    pending_nodes, pending_edges, completed_nodes, completed_edges
                )
                completed_nodes = merged.nodes
                completed_edges = merged.edges
                pending_nodes = []
                pending_edges = []

        logger.info(
            "Semantic mapping completed",
            extra={"datasource_id": datasource_id, "mapped_column_count": len(mapping_columns)},
        )
        return SemanticMappingResult(nodes=completed_nodes, edges=completed_edges)

    @staticmethod
    def _profile_for(
        column: GraphNode, profile_by_column: dict[str, ColumnProfile]
    ) -> ColumnProfile:
        """要求 Extract 的每个字段都有画像，缺失画像不允许绕过模型输入边界。"""

        profile = profile_by_column.get(column.id)
        if profile is None:
            raise SemanticMappingValidationError(
                "Column profile is unavailable for semantic mapping"
            )
        return profile

    def _build_batch(
        self,
        datasource_id: str,
        batch_index: int,
        inputs: Sequence[MappingColumnInput],
        response: SemanticMappingResponse,
    ) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
        """拒绝越界字段引用；模型响应字段已在客户端按严格契约验证。"""

        expected_column_ids = {column.column_id for column in inputs}
        concept_names = [concept.name for concept in response.concepts]
        if len(set(concept_names)) != len(concept_names):
            raise SemanticMappingValidationError(
                "Concept names must be unique within a mapping batch"
            )
        mapped_ids = [column_id for concept in response.concepts for column_id in concept.columns]
        if not set(mapped_ids) <= expected_column_ids:
            raise SemanticMappingValidationError(
                "Concept may reference only columns from its mapping batch"
            )
        if len({entity.name for entity in response.entities}) != len(response.entities):
            raise SemanticMappingValidationError(
                "Entity names must be unique within a mapping batch"
            )
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        concept_ids: dict[str, str] = {}
        for position, concept in enumerate(response.concepts, start=1):
            concept_id = make_graph_id(
                "concept", datasource_id, str(batch_index), str(position), concept.name
            )
            concept_ids[concept.name] = concept_id
            nodes.append(
                GraphNode(
                    id=concept_id,
                    type=DataLinkNodeType.CONCEPT,
                    name=concept.name,
                    description=concept.description,
                    aliases=tuple(dict.fromkeys(concept.aliases)),
                    properties={
                        "mapping_confidence": concept.confidence,
                        "provenance": "semantic_mapping",
                    },
                )
            )
            for column_id in dict.fromkeys(concept.columns):
                edges.append(
                    _semantic_edge(
                        column_id, concept_id, DataLinkEdgeType.REPRESENTS, concept.confidence
                    )
                )

        for position, entity in enumerate(response.entities, start=1):
            entity_id = make_graph_id(
                "entity", datasource_id, str(batch_index), str(position), entity.name
            )
            nodes.append(
                GraphNode(
                    id=entity_id,
                    type=DataLinkNodeType.ENTITY,
                    name=entity.name,
                    description=entity.description,
                    aliases=tuple(dict.fromkeys(entity.aliases)),
                    properties={
                        "mapping_confidence": entity.confidence,
                        "provenance": "semantic_mapping",
                    },
                )
            )
            for concept_name in dict.fromkeys(entity.concept_names):
                if concept_name not in concept_ids:
                    continue
                edges.append(
                    _semantic_edge(
                        entity_id,
                        concept_ids[concept_name],
                        DataLinkEdgeType.HAS_CONCEPT,
                        entity.confidence,
                    )
                )

        return tuple(nodes), tuple(edges)


def _semantic_edge(
    source_id: str, target_id: str, edge_type: DataLinkEdgeType, confidence: float
) -> GraphEdge:
    """创建只说明字段/概念组织关系的边，绝不把它当作 Join 证据。"""

    return GraphEdge(
        id=make_graph_id("edge", edge_type.value, source_id, target_id),
        source_id=source_id,
        target_id=target_id,
        type=edge_type,
        confidence=confidence,
        evidence=DataLinkEdgeEvidenceRead(
            kind="semantic_mapping",
            summary="Mapped by the configured DataLink semantic model",
        ),
        properties={"provenance": "semantic_mapping"},
    )


def _mapping_failure_kind(exc: Exception) -> str:
    """将 Map 异常归类为可观测但不含上游细节的稳定诊断标签。"""

    if isinstance(exc, ModelRequestTimeoutError):
        return "model_timeout"
    if isinstance(exc, ModelConfigurationError):
        return "model_configuration"
    if isinstance(exc, ModelUpstreamError):
        return "model_upstream"
    if isinstance(exc, ModelResponseFormatError):
        return "model_response_invalid"
    if isinstance(exc, SemanticMappingValidationError):
        return "mapping_validation"
    if isinstance(exc, ModelResponseError):
        return "model_response"
    return "unexpected"
