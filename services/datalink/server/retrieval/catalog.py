"""Version-local semantic catalog, independent of the bounded graph visualization."""

import json
from collections import Counter, defaultdict
from collections.abc import Callable

from contracts.datalink import (
    DataLinkAutomaticSemanticsRead,
    DataLinkBrowserNodeRead,
    DataLinkCatalogDetailRead,
    DataLinkCatalogItemRead,
    DataLinkCatalogMappedColumnRead,
    DataLinkCatalogMappingRead,
    DataLinkCatalogPrimaryMappingRead,
    DataLinkCatalogRead,
    DataLinkEdgeType,
    DataLinkNodeType,
    DataLinkRelationRead,
    DataLinkRelationsRead,
)

from server.graph.repository import GraphSnapshot
from server.models.graph import GraphNode, edge_enabled, graph_provenance
from server.models.profile import ColumnProfile


class SemanticCatalog:
    """Resolve mappings by typed edges and node IDs, never by display names."""

    def __init__(
        self,
        snapshot: GraphSnapshot,
        project: Callable[[GraphNode, ColumnProfile | None], DataLinkBrowserNodeRead],
    ) -> None:
        profiles = {profile.column_id: profile for profile in snapshot.profiles}
        self._raw_nodes = {node.id: node for node in snapshot.nodes}
        self.nodes = {node.id: project(node, profiles.get(node.id)) for node in snapshot.nodes}
        self.tables_by_column: dict[str, set[str]] = {}
        for edge in snapshot.edges:
            source, target = self.nodes.get(edge.source_id), self.nodes.get(edge.target_id)
            if (
                edge.type == DataLinkEdgeType.CONTAINS
                and source is not None
                and source.type == DataLinkNodeType.TABLE
                and target is not None
                and target.type == DataLinkNodeType.COLUMN
            ):
                self.tables_by_column.setdefault(target.id, set()).add(source.id)
        self.mappings: list[DataLinkCatalogMappingRead] = []
        entities = {}
        for edge in snapshot.edges:
            source, target = self.nodes.get(edge.source_id), self.nodes.get(edge.target_id)
            if (
                edge.type == DataLinkEdgeType.HAS_CONCEPT
                and source is not None
                and source.type == DataLinkNodeType.ENTITY
                and target is not None
                and target.type == DataLinkNodeType.CONCEPT
            ):
                entities.setdefault(target.id, []).append((source, edge))
        for edge in snapshot.edges:
            source, target = self.nodes.get(edge.source_id), self.nodes.get(edge.target_id)
            if (
                edge.type != DataLinkEdgeType.REPRESENTS
                or source is None
                or source.type != DataLinkNodeType.COLUMN
                or target is None
                or target.type != DataLinkNodeType.CONCEPT
            ):
                continue
            for entity, entity_edge in entities.get(target.id, [(None, None)]):
                self.mappings.append(
                    DataLinkCatalogMappingRead(
                        column=source,
                        concept=target,
                        entity=entity,
                        field_to_concept_confidence=edge.confidence,
                        entity_to_concept_confidence=entity_edge.confidence
                        if entity_edge
                        else None,
                        field_to_concept_provenance=graph_provenance(edge),
                        entity_to_concept_provenance=graph_provenance(entity_edge)
                        if entity_edge
                        else "unknown",
                        enabled=edge_enabled(edge)
                        and (entity_edge is None or edge_enabled(entity_edge)),
                    )
                )
        self.mappings = _keep_tightest_entities(self.mappings)
        self.mappings.sort(
            key=lambda m: (m.column.id, m.concept.id, m.entity.id if m.entity else "")
        )
        self.relations = []
        for edge in snapshot.edges:
            source, target = self.nodes.get(edge.source_id), self.nodes.get(edge.target_id)
            if source is None or target is None or edge.type == DataLinkEdgeType.CONTAINS:
                continue
            self.relations.append(
                DataLinkRelationRead(
                    id=edge.id,
                    source=source,
                    target=target,
                    type=edge.type,
                    confidence=edge.confidence,
                    evidence=edge.evidence,
                    provenance=graph_provenance(edge),
                    enabled=edge_enabled(edge),
                    join_eligible=(
                        edge_enabled(edge)
                        and edge.type in {DataLinkEdgeType.FOREIGN_KEY, DataLinkEdgeType.JOINABLE}
                        and source.type == target.type == DataLinkNodeType.COLUMN
                        and source.table is not None
                        and target.table is not None
                    ),
                )
            )
        self.relations.sort(key=lambda r: (r.source.id, r.target.id, r.type.value, r.id))
        self.mapping_counts: Counter[str] = Counter()
        self.relation_counts: Counter[str] = Counter()
        for mapping in self.mappings:
            self.mapping_counts.update(self._mapping_ids(mapping))
        for relation in self.relations:
            self.relation_counts.update({relation.source.id, relation.target.id})
        self._primary_mapping, self._mapping_search_text = _column_mapping_index(self.mappings)
        self._mapped_columns = _mapped_columns_index(self.mappings)

    def _mapping_ids(self, mapping: DataLinkCatalogMappingRead) -> set[str]:
        ids = {mapping.column.id, mapping.concept.id}
        if mapping.entity:
            ids.add(mapping.entity.id)
        ids.update(self.tables_by_column.get(mapping.column.id, set()))
        return ids

    def item(self, node: DataLinkBrowserNodeRead) -> DataLinkCatalogItemRead:
        raw = self._raw_nodes[node.id]
        return DataLinkCatalogItemRead(
            node=node,
            provenance=node.provenance,
            mapping_count=self.mapping_counts[node.id],
            relation_count=self.relation_counts[node.id],
            manual_created=raw.properties.get("manual_created") is True,
            can_reset_node=bool(
                raw.properties.get("manual_created") or raw.properties.get("semantic_override")
            ),
            can_reset_mapping=bool(raw.properties.get("mapping_override")),
            automatic=_automatic_semantics(raw),
            primary_mapping=(
                self._primary_mapping.get(node.id) if node.type == DataLinkNodeType.COLUMN else None
            ),
            mapped_columns=(
                self._mapped_columns.get(node.id, [])
                if node.type in {DataLinkNodeType.CONCEPT, DataLinkNodeType.ENTITY}
                else []
            ),
        )

    def list(
        self,
        datasource_id: str,
        graph_version: str,
        *,
        node_type: DataLinkNodeType | None,
        query: str | None,
        page: int,
        page_size: int,
    ) -> DataLinkCatalogRead:
        nodes = sorted(
            (
                node
                for node in self.nodes.values()
                if (node_type is None or node.type == node_type)
                and matches(node, query, extra=self._mapping_search_text.get(node.id, ""))
            ),
            key=lambda node: (node.type.value, node.table or "", node.name, node.id),
        )
        return DataLinkCatalogRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            items=[self.item(n) for n in nodes[(page - 1) * page_size : page * page_size]],
            page=page,
            page_size=page_size,
            total=len(nodes),
        )

    def detail(
        self, datasource_id: str, graph_version: str, node_id: str, *, page: int, page_size: int
    ) -> DataLinkCatalogDetailRead:
        mappings = [m for m in self.mappings if node_id in self._mapping_ids(m)]
        return DataLinkCatalogDetailRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            item=self.item(self.nodes[node_id]),
            mappings=mappings[(page - 1) * page_size : page * page_size],
            page=page,
            page_size=page_size,
            total=len(mappings),
        )

    def list_relations(
        self,
        datasource_id: str,
        graph_version: str,
        *,
        node_id: str | None,
        query: str | None,
        page: int,
        page_size: int,
    ) -> DataLinkRelationsRead:
        items = [
            r
            for r in self.relations
            if (node_id is None or node_id in {r.source.id, r.target.id})
            and (
                matches(r.source, query, extra=self._mapping_search_text.get(r.source.id, ""))
                or matches(r.target, query, extra=self._mapping_search_text.get(r.target.id, ""))
            )
        ]
        return DataLinkRelationsRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            items=items[(page - 1) * page_size : page * page_size],
            page=page,
            page_size=page_size,
            total=len(items),
        )

    def relation(self, relation_id: str) -> DataLinkRelationRead | None:
        for item in self.relations:
            if item.id == relation_id:
                return item
        return None


def matches(node: DataLinkBrowserNodeRead, query: str | None, extra: str = "") -> bool:
    text = " ".join(
        [node.name, node.table or "", node.description or "", *node.aliases, extra]
    ).casefold()
    return not query or query.strip().casefold() in text


def _overlap_score(
    entity_id: str,
    table: str | None,
    concepts_by_table: dict[str, set[str]],
    concepts_by_entity: dict[str, set[str]],
) -> int:
    """Count concepts this entity shares with columns on the same physical table."""

    return len(concepts_by_entity.get(entity_id, set()) & concepts_by_table.get(table or "", set()))


def _keep_tightest_entities(
    mappings: list[DataLinkCatalogMappingRead],
) -> list[DataLinkCatalogMappingRead]:
    """Keep entities that best explain a column's table; drop weaker shared-concept owners.

    Catalog walks REPRESENTS × HAS_CONCEPT. After concept merge, a foreign-key
    column and its home identifier share one concept, so every entity hanging
    that concept is painted onto every such column. Field 所属实体 is the
    business object the column describes, not every entity that reused the
    concept. Score is how many of the table's mapped concepts the entity also
    has; names and industry vocabulary are ignored. Ties stay (ADR 0013).
    Disabled rows are unchanged so revision still sees them.
    """

    concepts_by_table: dict[str, set[str]] = defaultdict(set)
    concepts_by_entity: dict[str, set[str]] = defaultdict(set)
    for mapping in mappings:
        if not mapping.enabled:
            continue
        concepts_by_table[mapping.column.table or ""].add(mapping.concept.id)
        if mapping.entity is not None:
            concepts_by_entity[mapping.entity.id].add(mapping.concept.id)

    grouped: dict[tuple[str, str], list[DataLinkCatalogMappingRead]] = defaultdict(list)
    kept: list[DataLinkCatalogMappingRead] = []
    for mapping in mappings:
        if mapping.entity is None or not mapping.enabled:
            kept.append(mapping)
            continue
        grouped[(mapping.column.id, mapping.concept.id)].append(mapping)

    for group in grouped.values():
        scores = [
            _overlap_score(
                mapping.entity.id if mapping.entity is not None else "",
                mapping.column.table,
                concepts_by_table,
                concepts_by_entity,
            )
            for mapping in group
        ]
        best = max(scores)
        kept.extend(mapping for mapping, score in zip(group, scores, strict=True) if score == best)
    return kept


def _column_mapping_index(
    mappings: list[DataLinkCatalogMappingRead],
) -> tuple[dict[str, DataLinkCatalogPrimaryMappingRead], dict[str, str]]:
    """Derive one list summary and search text per column from enabled mappings."""

    by_column: dict[str, list[DataLinkCatalogMappingRead]] = defaultdict(list)
    search_parts: dict[str, list[str]] = defaultdict(list)
    seen_tokens: dict[str, set[str]] = defaultdict(set)
    for mapping in mappings:
        if not mapping.enabled:
            continue
        by_column[mapping.column.id].append(mapping)
        tokens = [mapping.concept.name, mapping.concept.description or "", *mapping.concept.aliases]
        if mapping.entity is not None:
            tokens += [
                mapping.entity.name,
                mapping.entity.description or "",
                *mapping.entity.aliases,
            ]
        for token in tokens:
            text = token.strip()
            if text and text not in seen_tokens[mapping.column.id]:
                seen_tokens[mapping.column.id].add(text)
                search_parts[mapping.column.id].append(text)
    primary = {
        column_id: _primary_mapping_read(min(column_mappings, key=_primary_mapping_rank))
        for column_id, column_mappings in by_column.items()
    }
    search_text = {column_id: " ".join(parts) for column_id, parts in search_parts.items()}
    return primary, search_text


def _primary_mapping_rank(mapping: DataLinkCatalogMappingRead) -> tuple[object, ...]:
    field_confidence = mapping.field_to_concept_confidence
    entity_confidence = mapping.entity_to_concept_confidence
    return (
        field_confidence is None,
        -(field_confidence or 0.0),
        mapping.entity is None,
        entity_confidence is None,
        -(entity_confidence or 0.0),
        mapping.concept.id,
        mapping.entity.id if mapping.entity else "",
    )


def _primary_mapping_read(mapping: DataLinkCatalogMappingRead) -> DataLinkCatalogPrimaryMappingRead:
    return DataLinkCatalogPrimaryMappingRead(
        concept_name=mapping.concept.name,
        concept_description=mapping.concept.description,
        entity_name=mapping.entity.name if mapping.entity else None,
        field_to_concept_confidence=mapping.field_to_concept_confidence,
        provenance=mapping.field_to_concept_provenance,
    )


MAPPED_COLUMN_LIMIT = 50


def _mapped_columns_index(
    mappings: list[DataLinkCatalogMappingRead],
) -> dict[str, list[DataLinkCatalogMappedColumnRead]]:
    """Unique physical columns per concept/entity; cartesian HAS_CONCEPT does not duplicate."""

    unique: dict[str, dict[str, tuple[str, str | None, str]]] = defaultdict(dict)
    for mapping in mappings:
        if not mapping.enabled:
            continue
        entry = (mapping.column.id, mapping.column.table, mapping.column.name)
        unique[mapping.concept.id][mapping.column.id] = entry
        if mapping.entity is not None:
            unique[mapping.entity.id][mapping.column.id] = entry
    mapped: dict[str, list[DataLinkCatalogMappedColumnRead]] = {}
    for node_id, columns in unique.items():
        ordered = sorted(columns.values(), key=lambda item: (item[1] or "", item[2], item[0]))
        mapped[node_id] = [
            DataLinkCatalogMappedColumnRead(table=table, name=name)
            for _, table, name in ordered[:MAPPED_COLUMN_LIMIT]
        ]
    return mapped


def _automatic_semantics(node: GraphNode) -> DataLinkAutomaticSemanticsRead | None:
    raw = node.properties.get("semantic_override")
    if not raw:
        return None
    recorded = json.loads(str(raw))
    if not isinstance(recorded, dict):
        raise ValueError("semantic_override must be an object")

    def automatic(key: str) -> object:
        entry = recorded.get(key)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise ValueError("semantic_override fields must be objects")
        return entry.get("automatic")

    aliases = automatic("aliases")
    if aliases is None:
        aliases = []
    elif not isinstance(aliases, (list, tuple)):
        raise ValueError("semantic_override aliases must be a list")
    return DataLinkAutomaticSemanticsRead(
        name=automatic("name"),
        description=automatic("description"),
        aliases=list(aliases),
        semantic_type=automatic("semantic_type"),
    )
