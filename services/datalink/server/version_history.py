"""Immutable version restoration and explicit resolution of rebuild conflicts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from contracts.datalink import (
    DataLinkAutomaticSemanticsRead,
    DataLinkBuildStatus,
    DataLinkConflictResolution,
    DataLinkDraftChange,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkNodeType,
    DataLinkRebuildConflictRead,
    DataLinkRebuildConflictsRead,
    DataLinkResolveCandidateRequest,
    DataLinkRestoreRequest,
    DataLinkVersionDiffItemRead,
    DataLinkVersionDiffKind,
    DataLinkVersionDiffRead,
    DataLinkVersionDiffRelationRead,
    DataLinkVersionPublishRead,
    DataLinkVersionRead,
    DataLinkVersionsRead,
)
from pydantic import TypeAdapter

from server.graph.repository import GraphRepository, GraphSnapshot
from server.graph.storage import GraphStorage
from server.models.graph import GraphEdge, GraphNode, edge_enabled, graph_provenance
from server.revisions import RevisionConflict

_DIFF_LIMIT = 500
_JOIN_TYPES = {DataLinkEdgeType.FOREIGN_KEY, DataLinkEdgeType.JOINABLE}
_MAPPING_TYPES = {DataLinkEdgeType.REPRESENTS, DataLinkEdgeType.HAS_CONCEPT}


class VersionHistoryService:
    def __init__(self, storage: GraphStorage, repository: GraphRepository) -> None:
        self.storage = storage
        self.repository = repository

    def list_versions(
        self, datasource_id: str, page: int = 1, page_size: int = 30
    ) -> DataLinkVersionsRead:
        if page < 1 or not 1 <= page_size <= 100:
            raise RevisionConflict("Invalid version history pagination")
        with self.storage.connection() as conn:
            total = conn.execute(
                "SELECT count(*) FROM graph_builds WHERE datasource_id=?", (datasource_id,)
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT b.*, h.current_graph_version,
                (SELECT count(*) FROM semantic_rebuild_conflicts c
                  WHERE c.build_id=b.id) AS conflicts,
                (SELECT o.source_graph_version FROM semantic_version_operations o
                  WHERE o.datasource_id=b.datasource_id AND o.graph_version=b.graph_version
                  LIMIT 1) AS source_graph_version
                FROM graph_builds b LEFT JOIN datasource_graph_heads h
                  ON h.datasource_id=b.datasource_id
                WHERE b.datasource_id=? ORDER BY b.created_at DESC,b.id DESC LIMIT ? OFFSET ?""",
                (datasource_id, page_size, (page - 1) * page_size),
            ).fetchall()
        return DataLinkVersionsRead(
            datasource_id=datasource_id,
            page=page,
            page_size=page_size,
            total=total,
            items=[
                DataLinkVersionRead(
                    build_id=row["id"],
                    graph_version=row["graph_version"],
                    schema_revision=row["schema_revision"],
                    connection_revision=row["connection_revision"],
                    status=row["status"],
                    origin_kind=row["origin_kind"],
                    publication_state=row["publication_state"],
                    base_graph_version=row["base_graph_version"],
                    source_graph_version=row["source_graph_version"],
                    created_at=row["created_at"],
                    finished_at=row["finished_at"],
                    is_head=row["current_graph_version"] == row["graph_version"],
                    conflict_count=row["conflicts"],
                )
                for row in rows
            ],
        )

    def restore(
        self, datasource_id: str, request: DataLinkRestoreRequest
    ) -> DataLinkVersionPublishRead:
        with self.storage.transaction() as conn:
            replay = self._receipt(conn, datasource_id, "restore", request)
            if replay:
                return replay
            head = self._require_head(conn, datasource_id, request.expected_head)
            self._require_no_draft(conn, datasource_id)
            source = self.repository.get_completed_build(
                datasource_id, request.target_graph_version
            )
            if source is None:
                raise RevisionConflict("Restore requires a published version of this datasource")
            if (
                head["schema_revision"] != request.schema_revision
                or source.schema_revision != request.schema_revision
                or head["connection_revision"] != request.connection_revision
                or source.connection_revision != request.connection_revision
            ):
                raise RevisionConflict(
                    "Restore Schema revision does not match current Head",
                    DataLinkErrorCode.HEAD_STALE,
                )
            snapshot = self.repository.get_snapshot(source)
            return self._publish(conn, datasource_id, "restore", request, snapshot, "restore")

    def diff_version(self, datasource_id: str, graph_version: str) -> DataLinkVersionDiffRead:
        build = self.repository.get_build_by_graph_version(graph_version)
        if build is None:
            raise RevisionConflict(
                "Graph version does not exist", DataLinkErrorCode.GRAPH_VERSION_NOT_FOUND
            )
        if build.datasource_id != datasource_id:
            raise RevisionConflict(
                "Graph version does not belong to this datasource",
                DataLinkErrorCode.DATASOURCE_MISMATCH,
            )
        if build.status != DataLinkBuildStatus.COMPLETED:
            raise RevisionConflict("Version is not completed")
        current = self.repository.get_snapshot(build)
        base_version = build.base_graph_version or self._previous_published_version(
            datasource_id, graph_version
        )
        items: list[DataLinkVersionDiffItemRead] = []
        if base_version:
            base_build = self.repository.get_completed_build(datasource_id, base_version)
            if base_build is None:
                raise RevisionConflict(
                    "Base graph is unavailable", DataLinkErrorCode.GRAPH_VERSION_NOT_FOUND
                )
            items = _snapshot_diff(self.repository.get_snapshot(base_build), current)
        truncated = len(items) > _DIFF_LIMIT
        return DataLinkVersionDiffRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            base_graph_version=base_version,
            origin_kind=build.origin_kind,
            publication_state=build.publication_state,
            items=items[:_DIFF_LIMIT],
            truncated=truncated,
        )

    def candidate_snapshot(self, datasource_id: str, graph_version: str) -> GraphSnapshot:
        build = self.repository.get_build_by_graph_version(graph_version)
        if (
            build is None
            or build.datasource_id != datasource_id
            or build.status != DataLinkBuildStatus.COMPLETED
            or build.publication_state != "candidate"
            or not build.base_graph_version
        ):
            raise RevisionConflict("A completed candidate for this datasource is required")
        return self.repository.get_snapshot(build)

    def list_conflicts(
        self, datasource_id: str, candidate_version: str
    ) -> DataLinkRebuildConflictsRead:
        candidate = self.candidate_snapshot(datasource_id, candidate_version)
        base = self._candidate_base(candidate)
        nodes, edges = {n.id: n for n in base.nodes}, {e.id: e for e in base.edges}
        with self.storage.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM semantic_rebuild_conflicts WHERE build_id=? ORDER BY object_key",
                (candidate.build.id,),
            ).fetchall()
        items = []
        for row in rows:
            key = row["object_key"]
            node, edge = nodes.get(key), edges.get(key)
            if node is None and edge is None:
                raise RevisionConflict("Conflict source object is unavailable")
            items.append(
                DataLinkRebuildConflictRead(
                    object_key=key,
                    object_kind=(
                        "mapping"
                        if "mapping" in str(row["reason"]).lower()
                        else ("node" if node else "relation")
                    ),
                    name=node.name
                    if node
                    else f"{nodes[edge.source_id].name} / {nodes[edge.target_id].name}",
                    reason=row["reason"],
                    node_type=node.type if node else None,
                    edge_type=edge.type if edge else None,
                )
            )
        return DataLinkRebuildConflictsRead(
            datasource_id=datasource_id,
            candidate_graph_version=candidate_version,
            base_graph_version=base.build.graph_version,
            schema_revision=candidate.build.schema_revision,
            items=items,
        )

    def resolve_candidate(
        self, datasource_id: str, request: DataLinkResolveCandidateRequest
    ) -> DataLinkVersionPublishRead:
        with self.storage.transaction() as conn:
            replay = self._receipt(conn, datasource_id, "resolve_candidate", request)
            if replay:
                return replay
            self._require_head(conn, datasource_id, request.expected_head)
            self._require_no_draft(conn, datasource_id)
            candidate = self.candidate_snapshot(datasource_id, request.candidate_graph_version)
            if candidate.build.base_graph_version != request.expected_head:
                raise RevisionConflict("Candidate base is stale", DataLinkErrorCode.HEAD_STALE)
            if (
                candidate.build.schema_revision != request.schema_revision
                or candidate.build.connection_revision != request.connection_revision
                or self._candidate_base(candidate).build.connection_revision
                != request.connection_revision
            ):
                raise RevisionConflict(
                    "Candidate Schema revision is stale", DataLinkErrorCode.HEAD_STALE
                )
            keys = {
                row[0]
                for row in conn.execute(
                    "SELECT object_key FROM semantic_rebuild_conflicts WHERE build_id=?",
                    (candidate.build.id,),
                )
            }
            supplied = [r.object_key for r in request.resolutions]
            if len(set(supplied)) != len(supplied) or set(supplied) != keys:
                raise RevisionConflict("Resolve every candidate conflict exactly once")
            snapshot = _resolve(candidate, self._candidate_base(candidate), request.resolutions)
            return self._publish(
                conn, datasource_id, "resolve_candidate", request, snapshot, "manual"
            )

    def _candidate_base(self, candidate: GraphSnapshot) -> GraphSnapshot:
        build = self.repository.get_completed_build(
            candidate.build.datasource_id, candidate.build.base_graph_version
        )
        if build is None:
            raise RevisionConflict("Candidate base is unavailable", DataLinkErrorCode.HEAD_STALE)
        return self.repository.get_snapshot(build)

    def _previous_published_version(self, datasource_id: str, graph_version: str) -> str | None:
        with self.storage.connection() as conn:
            row = conn.execute(
                "SELECT result_json FROM semantic_version_operations "
                "WHERE datasource_id=? AND graph_version=?",
                (datasource_id, graph_version),
            ).fetchone()
        if row is None:
            return None
        return DataLinkVersionPublishRead.model_validate_json(
            row["result_json"]
        ).previous_graph_version

    @staticmethod
    def _require_head(conn: sqlite3.Connection, datasource_id: str, expected: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT b.* FROM datasource_graph_heads h JOIN graph_builds b "
            "ON b.id=h.current_build_id "
            "WHERE h.datasource_id=? AND h.current_graph_version=? AND b.status='completed' "
            "AND b.publication_state='published'",
            (datasource_id, expected),
        ).fetchone()
        if row is None:
            raise RevisionConflict("Graph Head is stale", DataLinkErrorCode.HEAD_STALE)
        return row

    @staticmethod
    def _require_no_draft(conn: sqlite3.Connection, datasource_id: str) -> None:
        draft = conn.execute(
            "SELECT changes_json FROM semantic_drafts WHERE datasource_id=? AND status='active'",
            (datasource_id,),
        ).fetchone()
        if draft is None:
            return
        if TypeAdapter(list[DataLinkDraftChange]).validate_json(draft["changes_json"]):
            raise RevisionConflict(
                "Publish or discard the active draft first", DataLinkErrorCode.DRAFT_STALE
            )
        conn.execute(
            "UPDATE semantic_drafts SET status='discarded' WHERE datasource_id=?", (datasource_id,)
        )

    @staticmethod
    def _receipt(
        conn: sqlite3.Connection,
        datasource_id: str,
        operation: str,
        request: DataLinkRestoreRequest | DataLinkResolveCandidateRequest,
    ) -> DataLinkVersionPublishRead | None:
        row = conn.execute(
            "SELECT * FROM semantic_version_operations WHERE datasource_id=? AND idempotency_key=?",
            (datasource_id, request.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or json.loads(row["request_json"]) != request.model_dump(
            mode="json"
        ):
            raise RevisionConflict(
                "Idempotency key belongs to another operation",
                DataLinkErrorCode.IDEMPOTENCY_CONFLICT,
            )
        return DataLinkVersionPublishRead.model_validate_json(row["result_json"])

    def _publish(
        self,
        conn: sqlite3.Connection,
        datasource_id: str,
        operation: str,
        request: DataLinkRestoreRequest | DataLinkResolveCandidateRequest,
        snapshot: GraphSnapshot,
        origin_kind: str,
    ) -> DataLinkVersionPublishRead:
        claim = self.repository.claim_build(
            datasource_id,
            snapshot.build.schema_revision,
            f"{operation}:{request.idempotency_key}",
            connection_revision=snapshot.build.connection_revision,
            connection=conn,
        )
        if not claim.should_execute:
            raise RevisionConflict(
                "A DataLink build is already running", DataLinkErrorCode.BUILD_ALREADY_RUNNING
            )
        conn.execute(
            "UPDATE graph_builds SET origin_kind=?,publication_state='published' WHERE id=?",
            (origin_kind, claim.build.id),
        )
        original = {n.id: n for n in self.repository.get_snapshot(snapshot.build).nodes}
        unchanged = {n.id for n in snapshot.nodes if original.get(n.id) == n}
        built = self.repository.store_completed_graph(
            claim.build.id,
            snapshot.nodes,
            snapshot.edges,
            snapshot.profiles,
            tuple(
                e
                for e in self.repository.get_embeddings(snapshot.build.id)
                if e.node_id in unchanged
            ),
            self.repository.get_pending_edges(snapshot.build.id),
            connection=conn,
        )
        result = DataLinkVersionPublishRead(
            datasource_id=datasource_id,
            graph_version=built.graph_version,
            previous_graph_version=request.expected_head,
            source_graph_version=snapshot.build.graph_version,
            origin_kind=origin_kind,
            idempotency_key=request.idempotency_key,
        )
        conn.execute(
            "INSERT INTO semantic_version_operations VALUES(?,?,?,?,?,?,?,?)",
            (
                datasource_id,
                request.idempotency_key,
                operation,
                snapshot.build.graph_version,
                built.graph_version,
                request.model_dump_json(),
                result.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )
        return result


def _resolve(
    candidate: GraphSnapshot, base: GraphSnapshot, resolutions: list[DataLinkConflictResolution]
) -> GraphSnapshot:
    nodes, edges = {n.id: n for n in candidate.nodes}, {e.id: e for e in candidate.edges}
    originals, original_edges = {n.id: n for n in base.nodes}, {e.id: e for e in base.edges}
    used_nodes = set()
    for resolution in resolutions:
        if resolution.action == "discard":
            continue
        old_node = originals.get(resolution.object_key)
        if old_node:
            target = nodes.get(resolution.node_id)
            if (
                target is None
                or target.type != old_node.type
                or target.id in used_nodes
                or target.properties.get("semantic_override")
            ):
                raise RevisionConflict("Node binding must have the same type and a unique target")
            used_nodes.add(target.id)
            nodes[target.id] = _rebind_node(old_node, target)
        elif resolution.target_ids is not None:
            source = nodes.get(resolution.object_key)
            targets = [nodes.get(t) for t in resolution.target_ids]
            if source is None or source.type not in {
                DataLinkNodeType.COLUMN,
                DataLinkNodeType.ENTITY,
            }:
                raise RevisionConflict("Mapping source must be a column or entity")
            if any(t is None or t.type != DataLinkNodeType.CONCEPT for t in targets):
                raise RevisionConflict("Mapping targets must be concepts")
            edge_type = (
                DataLinkEdgeType.HAS_CONCEPT
                if source.type == DataLinkNodeType.ENTITY
                else DataLinkEdgeType.REPRESENTS
            )
            edges = {
                k: e
                for k, e in edges.items()
                if not (e.source_id == source.id and e.type == edge_type)
            }
            for target in targets:
                edge_id = "manual_mapping_" + uuid5(NAMESPACE_URL, f"{source.id}:{target.id}").hex
                edges[edge_id] = GraphEdge(
                    edge_id,
                    source.id,
                    target.id,
                    edge_type,
                    None,
                    None,
                    {"provenance": "manual", "enabled": True, "validation_status": "unverified"},
                )
        else:
            old = original_edges.get(resolution.object_key)
            if old is None or resolution.node_id is not None:
                raise RevisionConflict("Relation conflict requires two endpoints")
            _rebind_edge(old, nodes, edges, resolution)
    return replace(candidate, nodes=tuple(nodes.values()), edges=tuple(edges.values()))


def _rebind_node(old: GraphNode, target: GraphNode) -> GraphNode:
    raw = old.properties.get("semantic_override")
    if not raw:
        raise RevisionConflict("Object has no recorded semantic override to rebind")
    override = json.loads(str(raw))
    values = {key: entry["value"] for key, entry in override.items()}
    if old.type in {DataLinkNodeType.COLUMN, DataLinkNodeType.TABLE}:
        values.pop("name", None)
    if "aliases" in values:
        values["aliases"] = tuple(values["aliases"])
    automatic = {
        key: {"automatic": getattr(target, key), "value": value} for key, value in values.items()
    }
    return replace(
        target,
        **values,
        properties={
            **target.properties,
            "provenance": "manual",
            "retrieval_mode": "keyword",
            "semantic_override": json.dumps(automatic),
            "rebound_from": old.id,
        },
    )


def _rebind_edge(
    old: GraphEdge,
    nodes: dict[str, GraphNode],
    edges: dict[str, GraphEdge],
    resolution: DataLinkConflictResolution,
) -> None:
    source, target = nodes.get(resolution.source_id), nodes.get(resolution.target_id)
    if (
        source is None
        or target is None
        or source.type != DataLinkNodeType.COLUMN
        or target.type != DataLinkNodeType.COLUMN
        or not source.table_name
        or not target.table_name
        or source.table_name == target.table_name
    ):
        raise RevisionConflict("Join bindings require real columns in different tables")
    if old.type not in {DataLinkEdgeType.FOREIGN_KEY, DataLinkEdgeType.JOINABLE}:
        raise RevisionConflict("Only Join relation conflicts accept column endpoints")
    if old.properties.get("provenance") != "manual":
        matches = [
            e
            for e in edges.values()
            if e.type == old.type and e.source_id == source.id and e.target_id == target.id
        ]
        if len(matches) != 1:
            raise RevisionConflict(
                "Bind the relation override to an existing relation of the same type"
            )
        edge = matches[0]
        enabled = old.properties.get("manual_enabled")
        if not isinstance(enabled, bool):
            raise RevisionConflict("Relation has no recorded manual override")
        edges[edge.id] = replace(
            edge,
            properties={
                **edge.properties,
                "enabled": enabled,
                "manual_enabled": enabled,
                "rebound_from": old.id,
            },
        )
        return
    if any(
        e.type == DataLinkEdgeType.JOINABLE and {e.source_id, e.target_id} == {source.id, target.id}
        for e in edges.values()
    ):
        raise RevisionConflict("A candidate already exists between the chosen columns")
    edge_id = "manual_" + uuid5(NAMESPACE_URL, f"resolve:{old.id}:{source.id}:{target.id}").hex
    edges[edge_id] = GraphEdge(
        edge_id,
        source.id,
        target.id,
        DataLinkEdgeType.JOINABLE,
        None,
        None,
        {
            **old.properties,
            "provenance": "manual",
            "validation_status": "unverified",
            "rebound_from": old.id,
        },
    )


def _snapshot_diff(
    base: GraphSnapshot, current: GraphSnapshot
) -> list[DataLinkVersionDiffItemRead]:
    before_nodes = {node.id: node for node in base.nodes}
    after_nodes = {node.id: node for node in current.nodes}
    items: list[DataLinkVersionDiffItemRead] = []
    for node_id in sorted(set(before_nodes) | set(after_nodes)):
        previous, latest = before_nodes.get(node_id), after_nodes.get(node_id)
        if previous is None and latest is not None:
            items.append(
                DataLinkVersionDiffItemRead(
                    kind=DataLinkVersionDiffKind.NODE_ADDED,
                    object_key=node_id,
                    object_kind="node",
                    name=_node_label(latest),
                    effective=_semantic_values(latest),
                )
            )
        elif latest is None and previous is not None:
            items.append(
                DataLinkVersionDiffItemRead(
                    kind=DataLinkVersionDiffKind.NODE_REMOVED,
                    object_key=node_id,
                    object_kind="node",
                    name=_node_label(previous),
                    automatic=_semantic_values(previous),
                )
            )
        elif previous is not None and latest is not None:
            automatic, effective = _semantic_values(previous), _semantic_values(latest)
            if automatic != effective:
                items.append(
                    DataLinkVersionDiffItemRead(
                        kind=DataLinkVersionDiffKind.NODE_UPDATED,
                        object_key=node_id,
                        object_kind="node",
                        name=_node_label(latest),
                        automatic=automatic,
                        effective=effective,
                    )
                )
    before_joins = {edge.id: edge for edge in base.edges if edge.type in _JOIN_TYPES}
    after_joins = {edge.id: edge for edge in current.edges if edge.type in _JOIN_TYPES}
    for edge_id in sorted(set(before_joins) | set(after_joins)):
        previous, latest = before_joins.get(edge_id), after_joins.get(edge_id)
        if previous is None and latest is not None:
            items.append(
                DataLinkVersionDiffItemRead(
                    kind=DataLinkVersionDiffKind.RELATION_ADDED,
                    object_key=edge_id,
                    object_kind="relation",
                    name=_relation_label(latest, after_nodes),
                    after_relation=_relation_view(latest, after_nodes),
                )
            )
        elif latest is None and previous is not None:
            items.append(
                DataLinkVersionDiffItemRead(
                    kind=DataLinkVersionDiffKind.RELATION_REMOVED,
                    object_key=edge_id,
                    object_kind="relation",
                    name=_relation_label(previous, before_nodes),
                    before_relation=_relation_view(previous, before_nodes),
                )
            )
        elif previous is not None and latest is not None:
            before_view, after_view = (
                _relation_view(previous, before_nodes),
                _relation_view(latest, after_nodes),
            )
            if before_view != after_view:
                items.append(
                    DataLinkVersionDiffItemRead(
                        kind=DataLinkVersionDiffKind.RELATION_UPDATED,
                        object_key=edge_id,
                        object_kind="relation",
                        name=_relation_label(latest, after_nodes),
                        before_relation=before_view,
                        after_relation=after_view,
                    )
                )
    before_maps = _mapping_targets(base.edges, before_nodes)
    after_maps = _mapping_targets(current.edges, after_nodes)
    for source_id in sorted(set(before_maps) | set(after_maps)):
        previous, latest = before_maps.get(source_id, ()), after_maps.get(source_id, ())
        if previous == latest:
            continue
        source = after_nodes.get(source_id) or before_nodes[source_id]
        items.append(
            DataLinkVersionDiffItemRead(
                kind=DataLinkVersionDiffKind.MAPPING_REPLACED,
                object_key=source_id,
                object_kind="mapping",
                name=_node_label(source),
                before_targets=list(previous),
                after_targets=list(latest),
            )
        )
    items.sort(key=lambda item: (item.kind.value, item.object_key))
    return items


def _semantic_values(node: GraphNode) -> DataLinkAutomaticSemanticsRead:
    return DataLinkAutomaticSemanticsRead(
        name=node.name,
        description=node.description,
        aliases=list(node.aliases),
        semantic_type=node.semantic_type,
    )


def _node_label(node: GraphNode) -> str:
    return f"{node.table_name}.{node.name}" if node.table_name else node.name


def _relation_label(edge: GraphEdge, nodes: dict[str, GraphNode]) -> str:
    source, target = nodes.get(edge.source_id), nodes.get(edge.target_id)
    left = _node_label(source) if source else edge.source_id
    right = _node_label(target) if target else edge.target_id
    return f"{left} → {right}"


def _relation_view(edge: GraphEdge, nodes: dict[str, GraphNode]) -> DataLinkVersionDiffRelationRead:
    source, target = nodes.get(edge.source_id), nodes.get(edge.target_id)
    return DataLinkVersionDiffRelationRead(
        source_id=edge.source_id,
        source_name=_node_label(source) if source else edge.source_id,
        target_id=edge.target_id,
        target_name=_node_label(target) if target else edge.target_id,
        type=edge.type,
        enabled=edge_enabled(edge),
        provenance=graph_provenance(edge),
    )


def _mapping_targets(
    edges: tuple[GraphEdge, ...], nodes: dict[str, GraphNode]
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for edge in edges:
        if edge.type not in _MAPPING_TYPES:
            continue
        target = nodes.get(edge.target_id)
        grouped.setdefault(edge.source_id, []).append(
            _node_label(target) if target else edge.target_id
        )
    return {key: tuple(sorted(values)) for key, values in grouped.items()}
