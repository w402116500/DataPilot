"""将 DataLink 子图投影为主 Agent 可见的安全语义上下文。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from contracts.datalink import (
    DataLinkEdgeEvidenceRead,
    DataLinkEdgeType,
    DataLinkNodeType,
    DataLinkProvenance,
    DataLinkSemanticCatalog,
    DataLinkSemanticConcept,
    DataLinkSemanticContext,
    DataLinkSemanticEntity,
    DataLinkSemanticEntityMapping,
    DataLinkSemanticEvidence,
    DataLinkSemanticField,
    DataLinkSemanticJoinPath,
    DataLinkSemanticJoinPathStep,
    DataLinkSemanticMapping,
    DataLinkSemanticRelationship,
)
from pydantic import ValidationError

from agent_runtime.contracts import DataLinkExploreResponse

_SLIM_ALIAS_LIMIT = 8


def slim_datalink_tool_observation_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Replace full semantic_context in a ToolMessage dump; leave other tools unchanged."""

    slimmed = dict(payload)
    summary = slimmed.get("summary")
    if not isinstance(summary, dict):
        return slimmed
    context = summary.get("semantic_context")
    if not isinstance(context, (Mapping, DataLinkSemanticContext)):
        return slimmed
    slimmed["summary"] = {
        **summary,
        "semantic_context": slim_datalink_semantic_context_for_model(context),
    }
    return slimmed


def slim_datalink_semantic_context_for_model(
    context: DataLinkSemanticContext | Mapping[str, Any],
) -> dict[str, Any]:
    """Keep actionable field/mapping names for the next Agent turn, drop catalog text."""

    semantic = _coerce_semantic_context(context)
    if semantic is None:
        return dict(context) if isinstance(context, Mapping) else {}
    return {
        "mode": semantic.mode,
        "graph_version": semantic.graph_version,
        "fields": [
            {
                "table": field.table,
                "column": field.column,
                "semantic_type": field.semantic_type,
                "provenance": field.provenance,
                "aliases": list(field.aliases[:_SLIM_ALIAS_LIMIT]),
                "semantic_mappings": [
                    {
                        "concept": mapping.concept.name,
                        "concept_provenance": mapping.concept.provenance,
                        "provenance": mapping.provenance,
                        "field_to_concept_confidence": mapping.field_to_concept_confidence,
                        "entities": [
                            {
                                "name": entity_mapping.entity.name,
                                "entity_provenance": entity_mapping.entity.provenance,
                                "provenance": entity_mapping.provenance,
                                "entity_to_concept_confidence": (
                                    entity_mapping.entity_to_concept_confidence
                                ),
                            }
                            for entity_mapping in mapping.entities
                        ],
                    }
                    for mapping in field.semantic_mappings
                ],
            }
            for field in semantic.fields
        ],
        "relationships": [
            {
                "source_table": relationship.source_table,
                "source_column": relationship.source_column,
                "target_table": relationship.target_table,
                "target_column": relationship.target_column,
                "edge_type": relationship.edge_type,
                "confidence": relationship.confidence,
                "provenance": relationship.provenance,
            }
            for relationship in semantic.relationships
        ],
        "join_paths": [
            {
                "tables": list(path.tables),
                "confidence": path.confidence,
                "provenance": path.provenance,
                "steps": [
                    {
                        "source_table": step.source_table,
                        "source_column": step.source_column,
                        "target_table": step.target_table,
                        "target_column": step.target_column,
                        "edge_type": step.edge_type,
                        "confidence": step.confidence,
                        "provenance": step.provenance,
                    }
                    for step in path.steps
                ],
            }
            for path in semantic.join_paths
        ],
    }


def _coerce_semantic_context(
    context: DataLinkSemanticContext | Mapping[str, Any],
) -> DataLinkSemanticContext | None:
    if isinstance(context, DataLinkSemanticContext):
        return context
    try:
        return DataLinkSemanticContext.model_validate(context, strict=True)
    except ValidationError:
        return None


def project_datalink_semantic_context(
    response: DataLinkExploreResponse,
) -> DataLinkSemanticContext:
    """去掉内部节点 ID 和画像样例，保留可核对的物理字段语义。"""

    explore = response.result
    node_by_id = {node.id: node for node in explore.nodes}
    columns_by_id = {
        node.id: node
        for node in explore.nodes
        if node.type is DataLinkNodeType.COLUMN and node.table is not None
    }
    concept_mappings_by_column: dict[
        str, list[tuple[DataLinkSemanticConcept, float | None, str, DataLinkProvenance]]
    ] = {}
    entity_mappings_by_concept: dict[str, list[DataLinkSemanticEntityMapping]] = {}
    for edge in explore.edges:
        if not edge.enabled:
            continue
        if edge.type is DataLinkEdgeType.REPRESENTS:
            source = columns_by_id.get(edge.source)
            concept = node_by_id.get(edge.target)
            if (
                source is not None
                and concept is not None
                and concept.type is DataLinkNodeType.CONCEPT
            ):
                concept_mappings_by_column.setdefault(source.id, []).append(
                    (
                        DataLinkSemanticConcept(
                            name=concept.name,
                            description=concept.description,
                            aliases=list(concept.aliases),
                            provenance=concept.provenance,
                        ),
                        edge.confidence,
                        concept.id,
                        edge.provenance,
                    )
                )
        elif edge.type is DataLinkEdgeType.HAS_CONCEPT:
            entity = node_by_id.get(edge.source)
            concept = node_by_id.get(edge.target)
            if (
                entity is not None
                and entity.type is DataLinkNodeType.ENTITY
                and concept is not None
                and concept.type is DataLinkNodeType.CONCEPT
            ):
                entity_mappings_by_concept.setdefault(concept.id, []).append(
                    DataLinkSemanticEntityMapping(
                        entity=DataLinkSemanticEntity(
                            name=entity.name,
                            description=entity.description,
                            aliases=list(entity.aliases),
                            provenance=entity.provenance,
                        ),
                        entity_to_concept_confidence=edge.confidence,
                        provenance=edge.provenance,
                    )
                )

    fields = [
        DataLinkSemanticField(
            table=column.table,
            column=column.name,
            description=column.description,
            semantic_type=column.semantic_type,
            aliases=list(column.aliases),
            provenance=column.provenance,
            semantic_mappings=_semantic_mappings_for_column(
                concept_mappings_by_column.get(column.id, ()), entity_mappings_by_concept
            ),
        )
        for column in columns_by_id.values()
    ]
    fields.sort(key=lambda field: (field.table, field.column))

    relationship_by_key: dict[tuple[str, str, str, str, str], DataLinkSemanticRelationship] = {}
    for edge in explore.edges:
        if not edge.enabled or edge.type not in {
            DataLinkEdgeType.FOREIGN_KEY,
            DataLinkEdgeType.JOINABLE,
        }:
            continue
        source = columns_by_id.get(edge.source)
        target = columns_by_id.get(edge.target)
        if source is None or target is None:
            continue
        relationship = DataLinkSemanticRelationship(
            source_table=source.table,
            source_column=source.name,
            target_table=target.table,
            target_column=target.name,
            edge_type=edge.type.value,
            confidence=edge.confidence,
            provenance=edge.provenance,
            evidence=_project_datalink_evidence(edge.evidence),
        )
        key = (
            relationship.source_table,
            relationship.source_column,
            relationship.target_table,
            relationship.target_column,
            relationship.edge_type,
        )
        current = relationship_by_key.get(key)
        if current is None or _confidence_rank(relationship.confidence) < _confidence_rank(
            current.confidence
        ):
            relationship_by_key[key] = relationship
    relationships = sorted(
        relationship_by_key.values(),
        key=lambda item: (
            item.source_table,
            item.source_column,
            item.target_table,
            item.target_column,
            item.edge_type,
        ),
    )

    join_paths = [
        DataLinkSemanticJoinPath(
            tables=list(path.tables),
            steps=[
                DataLinkSemanticJoinPathStep(
                    source_table=step.source_table,
                    source_column=step.source_column,
                    target_table=step.target_table,
                    target_column=step.target_column,
                    edge_type=step.edge_type.value,
                    confidence=step.confidence,
                    provenance=step.provenance,
                    evidence=_project_datalink_evidence(step.evidence),
                )
                for step in path.steps
            ],
            confidence=path.confidence,
            provenance=path.provenance,
            evidence=path.evidence,
        )
        for path in explore.join_paths
    ]
    return DataLinkSemanticContext(
        mode="cached" if response.cache_hit else "live",
        graph_version=explore.graph_version,
        semantic_catalog=_semantic_catalog_for_fields(fields),
        fields=fields,
        relationships=relationships,
        join_paths=join_paths,
        warnings=list(explore.warnings),
    )


def _semantic_mappings_for_column(
    concepts: Sequence[tuple[DataLinkSemanticConcept, float | None, str, DataLinkProvenance]],
    entities_by_concept: Mapping[str, Sequence[DataLinkSemanticEntityMapping]],
) -> list[DataLinkSemanticMapping]:
    mappings = [
        DataLinkSemanticMapping(
            concept=concept,
            field_to_concept_confidence=confidence,
            provenance=provenance,
            entities=sorted(
                entities_by_concept.get(concept_id, ()),
                key=lambda item: (
                    _confidence_rank(item.entity_to_concept_confidence),
                    item.entity.name,
                ),
            ),
        )
        for concept, confidence, concept_id, provenance in concepts
    ]
    return sorted(
        mappings,
        key=lambda item: (_confidence_rank(item.field_to_concept_confidence), item.concept.name),
    )


def _semantic_catalog_for_fields(
    fields: Sequence[DataLinkSemanticField],
) -> DataLinkSemanticCatalog:
    concepts: dict[str, DataLinkSemanticConcept] = {}
    entities: dict[str, DataLinkSemanticEntity] = {}
    for field in fields:
        for mapping in field.semantic_mappings:
            concepts.setdefault(mapping.concept.model_dump_json(), mapping.concept)
            for entity_mapping in mapping.entities:
                entity = entity_mapping.entity
                entities.setdefault(entity.model_dump_json(), entity)
    return DataLinkSemanticCatalog(
        concepts=sorted(concepts.values(), key=lambda item: (item.name, item.description or "")),
        entities=sorted(entities.values(), key=lambda item: (item.name, item.description or "")),
    )


def _confidence_rank(confidence: float | None) -> tuple:
    return (1,) if confidence is None else (0, -confidence)


def _project_datalink_evidence(
    evidence: DataLinkEdgeEvidenceRead | None,
) -> DataLinkSemanticEvidence | None:
    if evidence is None:
        return None
    return DataLinkSemanticEvidence(kind=evidence.kind, summary=evidence.summary)
