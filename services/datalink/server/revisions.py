"""Typed semantic revisions committed atomically in DataLink's database."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from contracts.datalink import (
    DataLinkChangeType,
    DataLinkDraftChange,
    DataLinkDraftRead,
    DataLinkDraftSaveRequest,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkNodeType,
    DataLinkPublishRead,
    DataLinkPublishRequest,
)
from contracts.ids import make_id

from server.graph.repository import GraphBuildRecord, GraphRepository, GraphSnapshot
from server.graph.storage import GraphStorage
from server.models.graph import GraphEdge, GraphEmbedding, GraphNode, GraphPendingEdge
from server.models.profile import ColumnProfile


class RevisionConflict(Exception):
    def __init__(
        self,
        message: str = "DataLink draft or graph head is stale",
        code: DataLinkErrorCode = DataLinkErrorCode.REVISION_INVALID,
    ) -> None:
        self.code = code
        super().__init__(message)


class RevisionService:
    def __init__(self, storage: GraphStorage, repository: GraphRepository) -> None:
        self.storage = storage
        self.repository = repository

    def store_rebuilt_graph(
        self,
        build: GraphBuildRecord,
        nodes: tuple[GraphNode, ...],
        edges: tuple[GraphEdge, ...],
        profiles: tuple[ColumnProfile, ...],
        embeddings: tuple[GraphEmbedding, ...],
        pending_edges: tuple[GraphPendingEdge, ...] = (),
    ) -> GraphBuildRecord:
        automatic = {node.id: node for node in nodes}
        conflicts: dict[str, str] = {}
        if build.base_graph_version:
            previous = self.repository.get_completed_build(
                build.datasource_id, build.base_graph_version
            )
            if previous is None:
                raise RevisionConflict("Rebuild base is unavailable", DataLinkErrorCode.HEAD_STALE)
            if previous.connection_revision == build.connection_revision:
                nodes, edges, conflicts = _inherit_overrides(
                    self.repository.get_snapshot(previous), nodes, edges
                )
        # Automatic embeddings cannot describe newly inherited manual text.
        embeddings = tuple(
            e
            for e in embeddings
            if automatic.get(e.node_id) == next((n for n in nodes if n.id == e.node_id), None)
        )
        with self.storage.transaction() as conn:
            conn.execute(
                "UPDATE graph_builds SET publication_state=? WHERE id=?",
                ("candidate" if conflicts else "published", build.id),
            )
            conn.executemany(
                "INSERT INTO semantic_rebuild_conflicts VALUES(?,?,?)",
                [(build.id, key, reason) for key, reason in conflicts.items()],
            )
            return self.repository.store_completed_graph(
                build.id,
                nodes,
                edges,
                profiles,
                embeddings,
                pending_edges,
                connection=conn,
            )

    def get_draft(self, datasource_id: str) -> DataLinkDraftRead | None:
        with self.storage.connection() as conn:
            row = conn.execute(
                "SELECT * FROM semantic_drafts WHERE datasource_id=?", (datasource_id,)
            ).fetchone()
        return _draft_from_row(row) if row else None

    def save_draft(
        self, datasource_id: str, request: DataLinkDraftSaveRequest
    ) -> DataLinkDraftRead:
        with self.storage.transaction() as conn:
            self._require_head(
                conn, datasource_id, request.base_graph_version, request.schema_revision
            )
            current = conn.execute(
                "SELECT * FROM semantic_drafts WHERE datasource_id=?", (datasource_id,)
            ).fetchone()
            expected = int(current["draft_revision"]) if current else None
            if request.expected_draft_revision != expected:
                raise RevisionConflict(
                    "Draft revision is stale; reload before saving", DataLinkErrorCode.DRAFT_STALE
                )
            base = self.repository.get_completed_build(datasource_id, request.base_graph_version)
            if base is None:
                raise RevisionConflict("Draft base graph is unavailable")
            snapshot = self.repository.get_snapshot(base)
            _apply_changes(snapshot.nodes, snapshot.edges, request.changes)
            revision, now = (expected or 0) + 1, _now()
            payload = [c.model_dump(mode="json") for c in request.changes]
            conn.execute(
                """INSERT INTO semantic_drafts VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(datasource_id) DO UPDATE SET
                base_graph_version=excluded.base_graph_version,
                schema_revision=excluded.schema_revision,draft_revision=excluded.draft_revision,
                status='active',changes_json=excluded.changes_json,updated_at=excluded.updated_at""",
                (
                    datasource_id,
                    request.base_graph_version,
                    request.schema_revision,
                    revision,
                    "active",
                    json.dumps(payload),
                    now,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO semantic_change_log VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        make_id("change"),
                        datasource_id,
                        revision,
                        c.change_type.value,
                        c.object_key,
                        c.model_dump_json(),
                        now,
                    )
                    for c in request.changes
                ],
            )
            return _draft_from_row(
                conn.execute(
                    "SELECT * FROM semantic_drafts WHERE datasource_id=?", (datasource_id,)
                ).fetchone()
            )

    def preview_snapshot(self, datasource_id: str, expected_draft_revision: int) -> GraphSnapshot:
        with self.storage.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM semantic_drafts WHERE datasource_id=?", (datasource_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or row["draft_revision"] != expected_draft_revision
            ):
                raise RevisionConflict("Draft revision is stale", DataLinkErrorCode.DRAFT_STALE)
            draft = _draft_from_row(row)
            self._require_head(conn, datasource_id, draft.base_graph_version, draft.schema_revision)
            build = self.repository.get_completed_build(datasource_id, draft.base_graph_version)
            if build is None:
                raise RevisionConflict("Draft base is unavailable", DataLinkErrorCode.HEAD_STALE)
            base = self.repository.get_snapshot(build)
            nodes, edges = _apply_changes(base.nodes, base.edges, draft.changes)
            return replace(base, nodes=nodes, edges=edges)

    def publish(self, datasource_id: str, request: DataLinkPublishRequest) -> DataLinkPublishRead:
        # Snapshot, Head CAS and receipt share a write lock and rollback boundary.
        # Retries cannot observe a placeholder receipt or a partial published graph.
        with self.storage.transaction() as conn:
            idem = conn.execute(
                "SELECT result_json FROM semantic_publish_log WHERE datasource_id=? "
                "AND idempotency_key=?",
                (datasource_id, request.idempotency_key),
            ).fetchone()
            if idem:
                result = DataLinkPublishRead.model_validate_json(idem["result_json"])
                if (
                    result.previous_graph_version != request.expected_head
                    or result.draft_revision != request.expected_draft_revision
                ):
                    raise RevisionConflict(
                        "Idempotency key belongs to a different publication",
                        DataLinkErrorCode.IDEMPOTENCY_CONFLICT,
                    )
                return result
            row = conn.execute(
                "SELECT * FROM semantic_drafts WHERE datasource_id=?", (datasource_id,)
            ).fetchone()
            if row is None or row["status"] != "active":
                raise RevisionConflict(
                    "No active draft is available", DataLinkErrorCode.DRAFT_STALE
                )
            draft = _draft_from_row(row)
            if (
                draft.draft_revision != request.expected_draft_revision
                or draft.base_graph_version != request.expected_head
            ):
                raise RevisionConflict(
                    "Draft revision or base graph is stale", DataLinkErrorCode.DRAFT_STALE
                )
            self._require_head(conn, datasource_id, request.expected_head, draft.schema_revision)
            base_build = self.repository.get_completed_build(datasource_id, request.expected_head)
            if base_build is None:
                raise RevisionConflict("Draft base graph is unavailable")
            base = self.repository.get_snapshot(base_build)
            nodes, edges = _apply_changes(base.nodes, base.edges, draft.changes)
            originals = {node.id: node for node in base.nodes}
            changed = {node.id for node in nodes if node != originals.get(node.id)}
            embeddings = tuple(
                e for e in self.repository.get_embeddings(base_build.id) if e.node_id not in changed
            )
            claim = self.repository.claim_build(
                datasource_id,
                draft.schema_revision,
                f"manual:{request.idempotency_key}",
                connection_revision=base_build.connection_revision,
                connection=conn,
            )
            if not claim.should_execute:
                raise RevisionConflict(
                    "A DataLink build is already running", DataLinkErrorCode.BUILD_ALREADY_RUNNING
                )
            conn.execute(
                "UPDATE graph_builds SET origin_kind='manual', publication_state='published', "
                "base_graph_version=? WHERE id=?",
                (request.expected_head, claim.build.id),
            )
            built = self.repository.store_completed_graph(
                claim.build.id,
                nodes,
                edges,
                base.profiles,
                embeddings,
                self.repository.get_pending_edges(base_build.id),
                connection=conn,
            )
            result = DataLinkPublishRead(
                datasource_id=datasource_id,
                graph_version=built.graph_version,
                previous_graph_version=request.expected_head,
                draft_revision=request.expected_draft_revision,
                idempotency_key=request.idempotency_key,
            )
            conn.execute(
                "INSERT INTO semantic_publish_log VALUES(?,?,?,?)",
                (datasource_id, request.idempotency_key, result.model_dump_json(), _now()),
            )
            conn.execute(
                "UPDATE semantic_drafts SET status='published',updated_at=? WHERE datasource_id=?",
                (_now(), datasource_id),
            )
            return result

    @staticmethod
    def _require_head(
        conn: sqlite3.Connection, datasource_id: str, version: str, schema_revision: int
    ) -> None:
        row = conn.execute(
            "SELECT b.schema_revision FROM datasource_graph_heads h JOIN graph_builds b "
            "ON b.id=h.current_build_id WHERE h.datasource_id=? AND h.current_graph_version=? "
            "AND b.status='completed' AND b.publication_state='published'",
            (datasource_id, version),
        ).fetchone()
        if row is None or row["schema_revision"] != schema_revision:
            raise RevisionConflict(
                "Graph head or Schema revision is stale", DataLinkErrorCode.HEAD_STALE
            )


def _apply_changes(
    nodes: tuple[GraphNode, ...], edges: tuple[GraphEdge, ...], changes: list[DataLinkDraftChange]
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
    node_map, edge_map = {n.id: n for n in nodes}, {e.id: e for e in edges}
    for c in changes:
        if c.change_type == DataLinkChangeType.ADD_NODE:
            node_id = c.object_key
            if node_id in node_map:
                raise RevisionConflict("Semantic node already exists")
            node_type = DataLinkNodeType(c.node_type)
            node_map[node_id] = GraphNode(
                node_id,
                node_type,
                c.name or node_id,
                description=c.description,
                aliases=tuple(c.aliases or ()),
                properties={
                    "provenance": "manual",
                    "manual_created": True,
                    "retrieval_mode": "keyword",
                },
            )
        elif c.change_type == DataLinkChangeType.UPDATE_NODE:
            node = node_map.get(c.object_key)
            if node is None:
                raise RevisionConflict("Node does not exist")
            if (
                node.type in {DataLinkNodeType.COLUMN, DataLinkNodeType.TABLE}
                and c.name is not None
                and c.name != node.name
            ):
                raise RevisionConflict("Physical table and column names cannot be changed")
            node_map[node.id] = replace(
                node,
                name=c.name if c.name is not None else node.name,
                semantic_type=c.semantic_type
                if c.semantic_type is not None
                else node.semantic_type,
                description=c.description if c.description is not None else node.description,
                aliases=tuple(c.aliases) if c.aliases is not None else node.aliases,
                properties={
                    **node.properties,
                    "provenance": "manual",
                    "retrieval_mode": "keyword",
                    "semantic_override": _node_override(node, c),
                },
            )
        elif c.change_type == DataLinkChangeType.RESET_NODE:
            node = node_map.get(c.object_key)
            if node is None:
                raise RevisionConflict("Node does not exist")
            if node.properties.get("manual_created"):
                del node_map[node.id]
                edge_map = {
                    k: e
                    for k, e in edge_map.items()
                    if e.source_id != node.id and e.target_id != node.id
                }
                continue
            raw = node.properties.get("semantic_override")
            if not raw:
                raise RevisionConflict("Node has no manual override")
            original = json.loads(str(raw))
            values = {k: v.get("automatic") for k, v in original.items()}
            values = {k: v for k, v in values.items() if v is not None}
            if "aliases" in values:
                values["aliases"] = tuple(values["aliases"])
            properties = {
                k: v
                for k, v in node.properties.items()
                if k not in {"semantic_override", "provenance", "retrieval_mode"}
            }
            if node.type in {DataLinkNodeType.CONCEPT, DataLinkNodeType.ENTITY}:
                # Map objects must not become unrecorded after restoring automatic text.
                properties["provenance"] = "semantic_mapping"
            node_map[node.id] = replace(
                node,
                **values,
                properties=properties,
            )
        elif c.change_type == DataLinkChangeType.RESET_MAPPING:
            source = node_map.get(c.object_key)
            if source is None:
                raise RevisionConflict("Mapping source does not exist")
            raw = source.properties.get("mapping_override")
            if not raw:
                raise RevisionConflict("Mapping has no manual override")
            original = json.loads(str(raw))
            edge_map = {
                k: e
                for k, e in edge_map.items()
                if not (
                    e.source_id == source.id
                    and e.type in {DataLinkEdgeType.REPRESENTS, DataLinkEdgeType.HAS_CONCEPT}
                )
            }
            for item in original:
                edge_id = item["id"]
                edge_map[edge_id] = GraphEdge(
                    edge_id,
                    item["source_id"],
                    item["target_id"],
                    DataLinkEdgeType(item["type"]),
                    item.get("confidence"),
                    None,
                    item.get("properties", {}),
                )
            node_map[source.id] = replace(
                source,
                properties={k: v for k, v in source.properties.items() if k != "mapping_override"},
            )
        elif c.change_type == DataLinkChangeType.RESET_RELATION:
            edge = _require_relation(edge_map, c.object_key)
            if edge.properties.get("provenance") != "manual":
                raise RevisionConflict("Relation has no manual override")
            replacement = edge.properties.get("replaces")
            del edge_map[edge.id]
            if replacement and replacement in edge_map:
                edge_map[replacement] = replace(
                    edge_map[replacement],
                    properties={
                        **edge_map[replacement].properties,
                        "enabled": True,
                        "manual_enabled": True,
                    },
                )
        elif c.change_type == DataLinkChangeType.REPLACE_MAPPING:
            source = node_map.get(c.object_key)
            targets = [node_map.get(target) for target in (c.target_ids or [])]
            if source is None or any(
                target is None or target.type != DataLinkNodeType.CONCEPT for target in targets
            ):
                raise RevisionConflict("Mapping targets must be existing concepts")
            old_edges = [
                e
                for e in edge_map.values()
                if e.source_id == source.id
                and e.type in {DataLinkEdgeType.REPRESENTS, DataLinkEdgeType.HAS_CONCEPT}
            ]
            if not old_edges and source.type != DataLinkNodeType.COLUMN:
                raise RevisionConflict("Mapping source must be a column or entity")
            if source.type == DataLinkNodeType.ENTITY:
                edge_type = DataLinkEdgeType.HAS_CONCEPT
            else:
                edge_type = DataLinkEdgeType.REPRESENTS
            if "mapping_override" not in source.properties:
                original = [
                    dict(
                        id=e.id,
                        source_id=e.source_id,
                        target_id=e.target_id,
                        type=e.type.value,
                        confidence=e.confidence,
                        properties=dict(e.properties),
                    )
                    for e in old_edges
                ]
            else:
                original = json.loads(str(source.properties["mapping_override"]))
            edge_map = {k: e for k, e in edge_map.items() if e not in old_edges}
            for target in targets:
                edge_id = "manual_mapping_" + uuid5(NAMESPACE_URL, f"{source.id}:{target.id}").hex
                edge_map[edge_id] = GraphEdge(
                    edge_id,
                    source.id,
                    target.id,
                    edge_type,
                    None,
                    None,
                    {"provenance": "manual", "enabled": True, "validation_status": "unverified"},
                )
            node_map[source.id] = replace(
                source,
                properties={
                    **source.properties,
                    "mapping_override": json.dumps(original),
                    "provenance": "manual",
                    "retrieval_mode": "keyword",
                },
            )
        elif c.change_type in {
            DataLinkChangeType.DISABLE_RELATION,
            DataLinkChangeType.ENABLE_RELATION,
        }:
            edge = _require_relation(edge_map, c.object_key)
            edge_map[edge.id] = replace(
                edge,
                properties={
                    **edge.properties,
                    "enabled": c.change_type == DataLinkChangeType.ENABLE_RELATION,
                    "manual_enabled": c.change_type == DataLinkChangeType.ENABLE_RELATION,
                },
            )
        elif c.change_type in {
            DataLinkChangeType.ADD_RELATION,
            DataLinkChangeType.REPOINT_RELATION,
        }:
            old = (
                _require_relation(edge_map, c.object_key)
                if c.change_type == DataLinkChangeType.REPOINT_RELATION
                else None
            )
            source, target = node_map.get(c.source_id), node_map.get(c.target_id)
            if (
                source is None
                or target is None
                or source.type != DataLinkNodeType.COLUMN
                or target.type != DataLinkNodeType.COLUMN
                or not source.table_name
                or not target.table_name
                or source.table_name == target.table_name
            ):
                raise RevisionConflict("Relation endpoints must be columns in different tables")
            if c.relation_type not in {None, "joinable"}:
                raise RevisionConflict("Manual relations cannot declare database foreign keys")
            if any(
                e.type == DataLinkEdgeType.JOINABLE
                and {e.source_id, e.target_id} == {source.id, target.id}
                for e in edge_map.values()
            ):
                raise RevisionConflict("A candidate relation already exists between these columns")
            edge_id = "manual_" + uuid5(NAMESPACE_URL, c.object_key).hex
            if edge_id in edge_map:
                raise RevisionConflict("Manual relation key already exists")
            if old:
                edge_map[old.id] = replace(
                    old,
                    properties={
                        **old.properties,
                        "enabled": False,
                        "manual_enabled": False,
                        "replaced_by": edge_id,
                    },
                )
            edge_map[edge_id] = GraphEdge(
                edge_id,
                source.id,
                target.id,
                DataLinkEdgeType.JOINABLE,
                None,
                None,
                {
                    "enabled": c.enabled is True,
                    "provenance": "manual",
                    "validation_status": "unverified",
                    "replaces": old.id if old else None,
                },
            )
        else:
            raise RevisionConflict("Unsupported semantic change")
    return tuple(node_map.values()), tuple(edge_map.values())


def _require_relation(edges: dict[str, GraphEdge], key: str) -> GraphEdge:
    edge = edges.get(key)
    if edge is None or edge.type not in {DataLinkEdgeType.FOREIGN_KEY, DataLinkEdgeType.JOINABLE}:
        raise RevisionConflict("Join relation does not exist")
    return edge


def _node_override(node: GraphNode, change: DataLinkDraftChange) -> str:
    override = json.loads(str(node.properties.get("semantic_override", "{}")))
    for key in ("name", "description", "aliases", "semantic_type"):
        value = getattr(change, key)
        if value is not None:
            previous = override.get(key, {}).get("automatic", getattr(node, key))
            override[key] = {"automatic": previous, "value": value}
    return json.dumps(override)


def _binding_keys(nodes: tuple[GraphNode, ...], edges: tuple[GraphEdge, ...]) -> dict[str, tuple]:
    """Match physical bindings and semantic structure, never semantic labels or old IDs."""
    keys = {}
    for n in nodes:
        if n.type == DataLinkNodeType.COLUMN:
            keys[n.id] = (
                n.type.value,
                n.table_name,
                n.name,
                n.properties.get("dtype"),
                n.properties.get("nullable"),
                n.properties.get("is_primary_key"),
            )
    for n in nodes:
        if n.type == DataLinkNodeType.TABLE:
            columns = tuple(sorted((k for k in keys.values() if k[1] == n.name), key=repr))
            keys[n.id] = (n.type.value, columns) if columns else ()
    for kind, edge_kind in (
        (DataLinkNodeType.CONCEPT, DataLinkEdgeType.REPRESENTS),
        (DataLinkNodeType.ENTITY, DataLinkEdgeType.HAS_CONCEPT),
    ):
        for n in nodes:
            if n.type != kind:
                continue
            bindings = [
                keys.get(e.source_id if kind == DataLinkNodeType.CONCEPT else e.target_id)
                for e in edges
                if e.type == edge_kind
                and (
                    e.target_id == n.id if kind == DataLinkNodeType.CONCEPT else e.source_id == n.id
                )
            ]
            keys[n.id] = (
                (kind.value, tuple(sorted(bindings, key=repr)))
                if bindings and all(bindings)
                else ()
            )
    return keys


def _inherit_overrides(
    base: GraphSnapshot, nodes: tuple[GraphNode, ...], edges: tuple[GraphEdge, ...]
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...], dict[str, str]]:
    old_keys, new_keys = _binding_keys(base.nodes, base.edges), _binding_keys(nodes, edges)
    mapping = {}
    for old_id, key in old_keys.items():
        matches = [new_id for new_id, candidate in new_keys.items() if candidate == key]
        if key and len(matches) == 1 and list(old_keys.values()).count(key) == 1:
            mapping[old_id] = matches[0]
    node_map, edge_map, conflicts = {n.id: n for n in nodes}, {e.id: e for e in edges}, {}
    for old in base.nodes:
        override_json = old.properties.get("semantic_override")
        if not override_json:
            if old.properties.get("provenance") == "manual":
                conflicts[old.id] = "Manual object has no reusable physical binding record"
            continue
        target = node_map.get(mapping.get(old.id))
        if target is None:
            conflicts[old.id] = "Physical binding is missing, changed, split, merged or ambiguous"
            continue
        override = json.loads(str(override_json))
        values = {key: entry["value"] for key, entry in override.items()}
        if "aliases" in values:
            values["aliases"] = tuple(values["aliases"])
        current_override = {
            key: {"automatic": getattr(target, key), "value": value}
            for key, value in values.items()
        }
        node_map[target.id] = replace(
            target,
            **values,
            properties={
                **target.properties,
                "semantic_override": json.dumps(current_override),
                "provenance": "manual",
                "retrieval_mode": "keyword",
                "inherited_from": base.build.graph_version,
            },
        )
    for old in base.edges:
        manual = old.properties.get("provenance") == "manual"
        if not manual and "manual_enabled" not in old.properties:
            continue
        source, target = mapping.get(old.source_id), mapping.get(old.target_id)
        if source is None or target is None:
            conflicts[old.id] = "Relation endpoint binding is missing or ambiguous"
            continue
        matches = [
            e
            for e in edge_map.values()
            if e.type == old.type and e.source_id == source and e.target_id == target
        ]
        if manual:
            if matches or old.id in edge_map:
                conflicts[old.id] = "Manual relation overlaps a rebuilt relation"
                continue
            edge_map[old.id] = replace(
                old,
                source_id=source,
                target_id=target,
                properties={
                    **old.properties,
                    "inherited_from": base.build.graph_version,
                    "validation_status": "unverified",
                },
            )
        elif len(matches) != 1:
            conflicts[old.id] = "Original relation no longer has a unique match"
        else:
            edge = matches[0]
            edge_map[edge.id] = replace(
                edge,
                properties={
                    **edge.properties,
                    "enabled": old.properties["manual_enabled"],
                    "manual_enabled": old.properties["manual_enabled"],
                    "inherited_from": base.build.graph_version,
                },
            )
    return tuple(node_map.values()), tuple(edge_map.values()), conflicts


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _draft_from_row(row: sqlite3.Row) -> DataLinkDraftRead:
    return DataLinkDraftRead(
        datasource_id=row["datasource_id"],
        base_graph_version=row["base_graph_version"],
        schema_revision=row["schema_revision"],
        draft_revision=row["draft_revision"],
        status=row["status"],
        changes=json.loads(row["changes_json"]),
    )
