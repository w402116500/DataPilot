"""Revision concurrency, immutable versions, and manual provenance boundaries."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from contracts.datalink import (
    DataLinkDraftChange,
    DataLinkDraftSaveRequest,
    DataLinkEdgeType,
    DataLinkNodeType,
    DataLinkPublishRequest,
)

from server.graph.repository import GraphRepository
from server.graph.storage import GraphStorage
from server.models.graph import GraphEdge, GraphEmbedding, GraphNode, graph_provenance
from server.revisions import RevisionConflict, RevisionService, _apply_changes


@pytest.fixture
def graph(tmp_path: Path):
    repo = GraphRepository(GraphStorage(tmp_path / "graph.db"))
    repo.initialize()
    build = repo.claim_build("ds", 1).build
    nodes = (
        GraphNode("a", DataLinkNodeType.COLUMN, "id", "orders", aliases=("old",)),
        GraphNode("b", DataLinkNodeType.COLUMN, "id", "users"),
        GraphNode("c", DataLinkNodeType.COLUMN, "other_id", "users"),
    )
    edges = (GraphEdge("fk", "a", "b", DataLinkEdgeType.FOREIGN_KEY, 1.0),)
    build = repo.store_completed_graph(
        build.id, nodes, edges, (), (GraphEmbedding("a", "test", (1.0,), "old"),)
    )
    return repo, RevisionService(repo.storage, repo), build


def save(service, build, changes, revision=None):
    return service.save_draft(
        "ds",
        DataLinkDraftSaveRequest(
            base_graph_version=build.graph_version,
            schema_revision=1,
            expected_draft_revision=revision,
            changes=changes,
        ),
    )


def publish_request(build, revision=1, key="publish"):
    return DataLinkPublishRequest(
        expected_head=build.graph_version, expected_draft_revision=revision, idempotency_key=key
    )


def test_publish_is_atomic_idempotent_and_preserves_old_snapshot(graph, monkeypatch):
    repo, service, base = graph
    save(
        service,
        base,
        [
            {
                "change_type": "update_node",
                "object_key": "a",
                "description": "New meaning",
                "aliases": [],
            }
        ],
    )
    request = publish_request(base)
    original = repo._insert_edges

    def fail(*args):
        raise RuntimeError("injected storage failure")

    monkeypatch.setattr(repo, "_insert_edges", fail)
    with pytest.raises(RuntimeError):
        service.publish("ds", request)
    assert repo.get_head("ds").current_graph_version == base.graph_version
    with repo.storage.connection() as conn:
        assert conn.execute("SELECT count(*) FROM graph_builds").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM semantic_publish_log").fetchone()[0] == 0
    monkeypatch.setattr(repo, "_insert_edges", original)
    result = service.publish("ds", request)
    assert service.publish("ds", request) == result
    new = repo.get_completed_build("ds", result.graph_version)
    assert new.origin_kind == "manual"
    node = next(n for n in repo.get_snapshot(new).nodes if n.id == "a")
    assert node.aliases == () and node.description == "New meaning"
    assert repo.get_embeddings(new.id) == ()
    assert next(n for n in repo.get_snapshot(base).nodes if n.id == "a").aliases == ("old",)
    assert repo.get_embeddings(base.id)


def test_save_requires_cas_schema_and_current_head(graph):
    repo, service, base = graph
    change = [{"change_type": "disable_relation", "object_key": "fk"}]
    save(service, base, change)
    with pytest.raises(RevisionConflict):
        save(service, base, change)
    with pytest.raises(RevisionConflict):
        service.save_draft(
            "ds",
            DataLinkDraftSaveRequest(
                base_graph_version=base.graph_version,
                schema_revision=2,
                expected_draft_revision=1,
                changes=change,
            ),
        )
    save(service, base, change, 1)
    with pytest.raises(RevisionConflict):
        service.publish("ds", publish_request(base))
    running = repo.claim_build("ds", 1).build
    with pytest.raises(RevisionConflict):
        service.publish("ds", publish_request(base, 2))
    repo.complete_build(running.id)
    with pytest.raises(RevisionConflict):
        service.publish("ds", publish_request(base, 2))
    with repo.storage.connection() as conn:
        assert conn.execute("SELECT count(*) FROM semantic_change_log").fetchone()[0] == 2


def test_manual_repoint_is_disabled_nullable_and_keeps_replacement(graph):
    repo, service, base = graph
    save(
        service,
        base,
        [
            {
                "change_type": "repoint_relation",
                "object_key": "fk",
                "source_id": "a",
                "target_id": "c",
            }
        ],
    )
    result = service.publish("ds", publish_request(base))
    edges = repo.get_snapshot(repo.get_completed_build("ds", result.graph_version)).edges
    manual = next(e for e in edges if e.type == DataLinkEdgeType.JOINABLE)
    assert manual.confidence is None and manual.properties["enabled"] is False
    assert manual.properties["provenance"] == "manual" and manual.properties["replaces"] == "fk"
    original = next(e for e in edges if e.id == "fk")
    assert original.confidence == 1.0 and original.properties["enabled"] is False
    assert original.properties["replaced_by"] == manual.id


@pytest.mark.parametrize(
    "change",
    [
        {"change_type": "update_node", "object_key": "a", "name": "renamed"},
        {"change_type": "add_relation", "object_key": "x", "source_id": "b", "target_id": "c"},
        {"change_type": "add_relation", "object_key": "x", "source_id": "a", "target_id": "absent"},
    ],
)
def test_invalid_physical_edits_are_rejected(graph, change):
    _, service, base = graph
    with pytest.raises(RevisionConflict):
        save(service, base, [change])


def test_candidate_completion_does_not_publish_or_allow_agent_read(graph):
    repo, _, base = graph
    candidate = repo.claim_build("ds", 1).build
    repo.set_build_metadata(
        candidate.id,
        origin_kind="automated",
        publication_state="candidate",
        base_graph_version=base.graph_version,
    )
    candidate = repo.complete_build(candidate.id)
    assert repo.get_head("ds").current_graph_version == base.graph_version
    assert repo.get_completed_build("ds", candidate.graph_version) is None


def test_legacy_edge_nullability_migrates_preserving_graph(tmp_path):
    storage = GraphStorage(tmp_path / "legacy.db")
    schema = Path(__file__).parents[1] / "server" / "graph" / "schema.sql"
    with sqlite3.connect(storage.database_path) as conn:
        conn.executescript(
            schema.read_text().replace("confidence REAL CHECK", "confidence REAL NOT NULL CHECK")
        )
    repo = GraphRepository(storage)
    build = repo.claim_build("ds", 1).build
    repo.store_completed_graph(
        build.id,
        [
            GraphNode("a", DataLinkNodeType.COLUMN, "id", "a"),
            GraphNode("b", DataLinkNodeType.COLUMN, "id", "b"),
        ],
        [GraphEdge("e", "a", "b", DataLinkEdgeType.JOINABLE, 0.8)],
        (),
    )
    storage.initialize()
    storage.initialize()
    assert repo.get_snapshot(repo.get_build(build.id)).edges[0].confidence == 0.8
    with storage.connection() as conn:
        assert not next(
            r for r in conn.execute("PRAGMA table_info(edges)") if r["name"] == "confidence"
        )["notnull"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rebuild_inherits_physical_overrides_even_when_ids_change(graph):
    repo, service, base = graph
    save(
        service,
        base,
        [
            {
                "change_type": "update_node",
                "object_key": "a",
                "description": "Curated",
                "aliases": [],
            },
            {"change_type": "disable_relation", "object_key": "fk"},
            {
                "change_type": "add_relation",
                "object_key": "manual-a-c",
                "source_id": "a",
                "target_id": "c",
                "enabled": True,
            },
        ],
    )
    published = service.publish("ds", publish_request(base))
    automatic = repo.get_snapshot(base)
    new_nodes = tuple(
        replace(n, id="new-" + n.id, description="Fresh automatic") for n in automatic.nodes
    )
    new_edges = tuple(
        replace(e, id="new-" + e.id, source_id="new-" + e.source_id, target_id="new-" + e.target_id)
        for e in automatic.edges
    )
    rebuild = repo.claim_build("ds", 1).build
    completed = service.store_rebuilt_graph(rebuild, new_nodes, new_edges, (), ())
    assert completed.publication_state == "published"
    snapshot = repo.get_snapshot(completed)
    node = next(n for n in snapshot.nodes if n.id == "new-a")
    assert node.description == "Curated" and node.aliases == ()
    assert next(e for e in snapshot.edges if e.id == "new-fk").properties["enabled"] is False
    manual = next(e for e in snapshot.edges if e.type == DataLinkEdgeType.JOINABLE)
    assert manual.source_id == "new-a" and manual.target_id == "new-c"
    assert manual.confidence is None and manual.properties["enabled"] is True
    assert repo.get_completed_build("ds", published.graph_version) is not None


def test_rebuild_missing_binding_produces_candidate_with_conflict(graph):
    repo, service, base = graph
    save(
        service, base, [{"change_type": "update_node", "object_key": "a", "description": "Curated"}]
    )
    published = service.publish("ds", publish_request(base))
    automatic = repo.get_snapshot(base)
    rebuild = repo.claim_build("ds", 2).build
    # Even retaining the old node ID cannot prove a renamed physical column is the same field.
    changed = tuple(replace(n, name="renamed") if n.id == "a" else n for n in automatic.nodes)
    candidate = service.store_rebuilt_graph(rebuild, changed, automatic.edges, (), ())
    assert candidate.publication_state == "candidate"
    assert repo.get_head("ds").current_graph_version == published.graph_version
    with repo.storage.connection() as conn:
        assert (
            conn.execute(
                "SELECT object_key FROM semantic_rebuild_conflicts WHERE build_id=?",
                (candidate.id,),
            ).fetchone()[0]
            == "a"
        )


def test_preview_applies_draft_without_materializing_or_changing_head(graph):
    repo, service, base = graph
    save(service, base, [{"change_type": "disable_relation", "object_key": "fk"}])
    assert service.preview_snapshot("ds", 1).edges[0].properties["enabled"] is False
    assert "enabled" not in repo.get_snapshot(base).edges[0].properties
    with pytest.raises(RevisionConflict):
        service.preview_snapshot("ds", 2)
    assert repo.get_head("ds").current_graph_version == base.graph_version


def test_semantic_node_mapping_and_reset_restore_automatic_edges(graph):
    repo, service, base = graph
    nodes = (
        *repo.get_snapshot(base).nodes,
        GraphNode("concept", DataLinkNodeType.CONCEPT, "Customer", description="Automatic"),
        GraphNode("entity", DataLinkNodeType.ENTITY, "Customer", description="Automatic entity"),
    )
    # Replace the fixture's base snapshot with semantic nodes for this focused compiler test.
    edges = (
        *repo.get_snapshot(base).edges,
        GraphEdge("rep", "a", "concept", DataLinkEdgeType.REPRESENTS, 0.8),
        GraphEdge("has", "entity", "concept", DataLinkEdgeType.HAS_CONCEPT, 0.7),
    )
    add = {
        "change_type": "add_node",
        "object_key": "manual-concept",
        "node_type": "concept",
        "name": "Revenue",
        "description": "Curated revenue",
    }
    replace_mapping = {
        "change_type": "replace_mapping",
        "object_key": "a",
        "target_ids": ["manual-concept"],
    }
    changed_nodes, changed_edges = _apply_changes(
        nodes,
        edges,
        [DataLinkDraftChange.model_validate(change) for change in [add, replace_mapping]],
    )
    manual_node = next(node for node in changed_nodes if node.id == "manual-concept")
    assert manual_node.properties["manual_created"] is True
    assert not any(edge.id == "rep" for edge in changed_edges)
    manual_edge = next(edge for edge in changed_edges if edge.type == DataLinkEdgeType.REPRESENTS)
    assert manual_edge.target_id == "manual-concept" and manual_edge.confidence is None
    reset_nodes, reset_edges = _apply_changes(
        changed_nodes,
        changed_edges,
        [DataLinkDraftChange(change_type="reset_mapping", object_key="a")],
    )
    restored = next(edge for edge in reset_edges if edge.id == "rep")
    assert restored.target_id == "concept"
    assert "provenance" not in restored.properties
    assert graph_provenance(restored) == "unknown"
    assert not any(edge.target_id == "manual-concept" for edge in reset_edges)
    assert (
        next(node for node in reset_nodes if node.id == "a").properties.get("mapping_override")
        is None
    )


def test_reset_semantic_node_writes_back_mapping_provenance(graph):
    repo, _, base = graph
    snapshot = repo.get_snapshot(base)
    nodes = (
        *snapshot.nodes,
        GraphNode(
            "concept",
            DataLinkNodeType.CONCEPT,
            "Customer",
            description="Automatic",
            properties={"provenance": "semantic_mapping", "mapping_confidence": 0.8},
        ),
        GraphNode(
            "entity",
            DataLinkNodeType.ENTITY,
            "Customer",
            description="Automatic entity",
            properties={"provenance": "semantic_mapping"},
        ),
    )
    updated, _ = _apply_changes(
        nodes,
        snapshot.edges,
        [
            DataLinkDraftChange(
                change_type="update_node", object_key="concept", description="Curated"
            ),
            DataLinkDraftChange(
                change_type="update_node", object_key="entity", description="Curated entity"
            ),
        ],
    )
    assert (
        next(node for node in updated if node.id == "concept").properties["provenance"] == "manual"
    )
    restored, _ = _apply_changes(
        updated,
        snapshot.edges,
        [
            DataLinkDraftChange(change_type="reset_node", object_key="concept"),
            DataLinkDraftChange(change_type="reset_node", object_key="entity"),
        ],
    )
    concept = next(node for node in restored if node.id == "concept")
    entity = next(node for node in restored if node.id == "entity")
    assert concept.description == "Automatic"
    assert entity.description == "Automatic entity"
    assert concept.properties.get("semantic_override") is None
    assert "retrieval_mode" not in concept.properties
    assert concept.properties["provenance"] == "semantic_mapping"
    assert entity.properties["provenance"] == "semantic_mapping"
    assert graph_provenance(concept) == "semantic_mapping"


def test_reset_column_does_not_write_semantic_mapping(graph):
    repo, _, base = graph
    snapshot = repo.get_snapshot(base)
    updated, _ = _apply_changes(
        snapshot.nodes,
        snapshot.edges,
        [DataLinkDraftChange(change_type="update_node", object_key="a", description="Curated")],
    )
    restored, _ = _apply_changes(
        updated,
        snapshot.edges,
        [DataLinkDraftChange(change_type="reset_node", object_key="a")],
    )
    column = next(node for node in restored if node.id == "a")
    assert "provenance" not in column.properties
    assert graph_provenance(column) == "structural"


def test_reset_mapping_restores_recorded_semantic_mapping(graph):
    repo, _, base = graph
    snapshot = repo.get_snapshot(base)
    nodes = (
        *snapshot.nodes,
        GraphNode(
            "concept",
            DataLinkNodeType.CONCEPT,
            "Customer",
            properties={"provenance": "semantic_mapping"},
        ),
    )
    edges = (
        *snapshot.edges,
        GraphEdge(
            "rep",
            "a",
            "concept",
            DataLinkEdgeType.REPRESENTS,
            0.8,
            properties={"provenance": "semantic_mapping"},
        ),
    )
    changed_nodes, changed_edges = _apply_changes(
        nodes,
        edges,
        [
            DataLinkDraftChange(
                change_type="add_node",
                object_key="manual-concept",
                node_type="concept",
                name="Revenue",
            ),
            DataLinkDraftChange(
                change_type="replace_mapping",
                object_key="a",
                target_ids=["manual-concept"],
            ),
        ],
    )
    reset_nodes, reset_edges = _apply_changes(
        changed_nodes,
        changed_edges,
        [DataLinkDraftChange(change_type="reset_mapping", object_key="a")],
    )
    restored = next(edge for edge in reset_edges if edge.id == "rep")
    assert restored.target_id == "concept"
    assert restored.properties["provenance"] == "semantic_mapping"
    assert graph_provenance(restored) == "semantic_mapping"
    assert not any(edge.target_id == "manual-concept" for edge in reset_edges)
    assert (
        next(node for node in reset_nodes if node.id == "a").properties.get("mapping_override")
        is None
    )


def test_reset_manual_node_removes_its_edges(graph):
    repo, _, base = graph
    snapshot = repo.get_snapshot(base)
    changed_nodes, changed_edges = _apply_changes(
        snapshot.nodes,
        snapshot.edges,
        [
            DataLinkDraftChange(
                change_type="add_node",
                object_key="manual-entity",
                node_type="entity",
                name="Buyer",
            )
        ],
    )
    restored_nodes, restored_edges = _apply_changes(
        changed_nodes,
        changed_edges,
        [DataLinkDraftChange(change_type="reset_node", object_key="manual-entity")],
    )
    assert not any(node.id == "manual-entity" for node in restored_nodes)
    assert len(restored_edges) == len(snapshot.edges)
