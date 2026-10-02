"""Immutable restore and candidate resolution exercise real storage transactions."""

from dataclasses import replace

import pytest
from contracts.datalink import (
    DataLinkDraftSaveRequest,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkNodeType,
    DataLinkPublishRequest,
    DataLinkResolveCandidateRequest,
    DataLinkRestoreRequest,
    DataLinkVersionDiffKind,
)

from server.graph.repository import GraphRepository
from server.graph.storage import GraphStorage
from server.models.graph import GraphEdge, GraphEmbedding, GraphNode
from server.revisions import RevisionConflict, RevisionService
from server.version_history import VersionHistoryService


@pytest.fixture
def versions(tmp_path):
    repo = GraphRepository(GraphStorage(tmp_path / "graph.db"))
    repo.initialize()
    first = repo.claim_build("ds", 1).build
    first = repo.store_completed_graph(
        first.id,
        (
            GraphNode("a", DataLinkNodeType.COLUMN, "id", "orders", description="Automatic"),
            GraphNode("b", DataLinkNodeType.COLUMN, "id", "users"),
        ),
        (GraphEdge("fk", "a", "b", DataLinkEdgeType.FOREIGN_KEY, 1.0),),
        (),
        (GraphEmbedding("a", "test", (1.0,), "Automatic"),),
    )
    revisions = RevisionService(repo.storage, repo)
    revisions.save_draft(
        "ds",
        DataLinkDraftSaveRequest(
            base_graph_version=first.graph_version,
            schema_revision=1,
            changes=[{"change_type": "update_node", "object_key": "a", "description": "Curated"}],
        ),
    )
    published = revisions.publish(
        "ds",
        DataLinkPublishRequest(
            expected_head=first.graph_version,
            expected_draft_revision=1,
            idempotency_key="publish",
        ),
    )
    return repo, revisions, VersionHistoryService(repo.storage, repo), first, published


def restore_request(first, published, **kwargs):
    return DataLinkRestoreRequest(
        **{
            "target_graph_version": first.graph_version,
            "expected_head": published.graph_version,
            "schema_revision": 1,
            "idempotency_key": "restore",
            **kwargs,
        }
    )


def test_restore_creates_immutable_version_and_persisted_retry_receipt(versions):
    repo, _, history, first, published = versions
    request = restore_request(first, published)
    result = history.restore("ds", request)
    restarted = VersionHistoryService(GraphStorage(repo.storage.database_path), repo)
    assert restarted.restore("ds", request) == result
    assert result.graph_version not in {first.graph_version, published.graph_version}
    restored = repo.get_completed_build("ds", result.graph_version)
    assert restored.origin_kind == "restore"
    assert repo.get_snapshot(restored).nodes == repo.get_snapshot(first).nodes
    assert repo.get_embeddings(restored.id) == repo.get_embeddings(first.id)
    assert (
        next(
            n
            for n in repo.get_snapshot(
                repo.get_completed_build("ds", published.graph_version)
            ).nodes
            if n.id == "a"
        ).description
        == "Curated"
    )
    page = history.list_versions("ds", page_size=2)
    assert page.total == 3 and len(page.items) == 2
    assert page.items[0].is_head and page.items[0].source_graph_version == first.graph_version
    with pytest.raises(RevisionConflict):
        history.restore(
            "ds", restore_request(first, published, target_graph_version=published.graph_version)
        )


def test_restore_failure_rolls_back_build_head_and_receipt(versions, monkeypatch):
    repo, _, history, first, published = versions
    original = repo._insert_embeddings

    def fail(*args):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(repo, "_insert_embeddings", fail)
    with pytest.raises(RuntimeError):
        history.restore("ds", restore_request(first, published))
    assert repo.get_head("ds").current_graph_version == published.graph_version
    with repo.storage.connection() as conn:
        assert conn.execute("SELECT count(*) FROM graph_builds").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM semantic_version_operations").fetchone()[0] == 0
    monkeypatch.setattr(repo, "_insert_embeddings", original)
    history.restore("ds", restore_request(first, published))


def test_restore_rejects_stale_schema_head_running_build_and_active_draft(versions):
    repo, revisions, history, first, published = versions
    for overrides in ({"schema_revision": 2}, {"expected_head": first.graph_version}):
        with pytest.raises(RevisionConflict):
            history.restore("ds", restore_request(first, published, **overrides))
    running = repo.claim_build("ds", 1).build
    with pytest.raises(RevisionConflict):
        history.restore("ds", restore_request(first, published))
    repo.fail_build(running.id)
    revisions.save_draft(
        "ds",
        DataLinkDraftSaveRequest(
            base_graph_version=published.graph_version,
            schema_revision=1,
            expected_draft_revision=1,
            changes=[{"change_type": "disable_relation", "object_key": "fk"}],
        ),
    )
    with pytest.raises(RevisionConflict):
        history.restore("ds", restore_request(first, published))
    assert revisions.get_draft("ds").status == "active"


@pytest.mark.parametrize("operation", ["restore", "resolve"])
def test_cleared_draft_does_not_block_version_operation(versions, operation):
    repo, revisions, history, first, published = versions
    candidate = create_candidate(repo, revisions, first) if operation == "resolve" else None
    revisions.save_draft(
        "ds",
        DataLinkDraftSaveRequest(
            base_graph_version=published.graph_version,
            schema_revision=1,
            expected_draft_revision=1,
            changes=[],
        ),
    )
    if operation == "restore":
        history.restore("ds", restore_request(first, published))
    else:
        history.resolve_candidate(
            "ds",
            DataLinkResolveCandidateRequest(
                candidate_graph_version=candidate.graph_version,
                expected_head=published.graph_version,
                schema_revision=2,
                idempotency_key="resolve",
                resolutions=[{"object_key": "a", "action": "discard"}],
            ),
        )
    assert revisions.get_draft("ds").status == "discarded"


def create_candidate(repo, revisions, first):
    automatic = repo.get_snapshot(first)
    nodes = tuple(
        replace(n, id="new-a", name="order_id") if n.id == "a" else n for n in automatic.nodes
    )
    edges = tuple(replace(e, source_id="new-a") for e in automatic.edges)
    candidate = repo.claim_build("ds", 2).build
    return revisions.store_rebuilt_graph(candidate, nodes, edges, (), ())


@pytest.mark.parametrize(
    "resolution, description",
    [
        ({"object_key": "a", "action": "discard"}, "Automatic"),
        ({"object_key": "a", "action": "rebind", "node_id": "new-a"}, "Curated"),
    ],
)
def test_resolve_candidate_publishes_new_version_without_mutating_candidate(
    versions, resolution, description
):
    repo, revisions, history, first, published = versions
    candidate = create_candidate(repo, revisions, first)
    conflicts = history.list_conflicts("ds", candidate.graph_version)
    assert conflicts.items[0].object_key == "a" and conflicts.items[0].object_kind == "node"
    request = DataLinkResolveCandidateRequest(
        candidate_graph_version=candidate.graph_version,
        expected_head=published.graph_version,
        schema_revision=2,
        idempotency_key="resolve",
        resolutions=[resolution],
    )
    result = history.resolve_candidate("ds", request)
    assert history.resolve_candidate("ds", request) == result
    assert result.graph_version != candidate.graph_version
    resolved = repo.get_completed_build("ds", result.graph_version)
    assert (
        next(n for n in repo.get_snapshot(resolved).nodes if n.id == "new-a").description
        == description
    )
    assert repo.get_build(candidate.id).publication_state == "candidate"
    assert (
        next(n for n in repo.get_snapshot(candidate).nodes if n.id == "new-a").description
        == "Automatic"
    )
    assert repo.get_completed_build("ds", candidate.graph_version) is None


def test_candidate_resolution_requires_exact_conflict_set_and_real_target(versions):
    repo, revisions, history, first, published = versions
    candidate = create_candidate(repo, revisions, first)
    for resolutions in (
        [{"object_key": "wrong", "action": "discard"}],
        [{"object_key": "a", "action": "rebind", "node_id": "missing"}],
        [{"object_key": "a", "action": "discard"}] * 2,
    ):
        with pytest.raises(RevisionConflict):
            history.resolve_candidate(
                "ds",
                DataLinkResolveCandidateRequest(
                    candidate_graph_version=candidate.graph_version,
                    expected_head=published.graph_version,
                    schema_revision=2,
                    idempotency_key="resolve",
                    resolutions=resolutions,
                ),
            )
    assert repo.get_head("ds").current_graph_version == published.graph_version
    with pytest.raises(RevisionConflict):
        history.candidate_snapshot("other", candidate.graph_version)


def test_manual_relation_conflict_rebind_keeps_null_confidence_and_enabled_state(versions):
    repo, revisions, history, first, published = versions
    revisions.save_draft(
        "ds",
        DataLinkDraftSaveRequest(
            base_graph_version=published.graph_version,
            schema_revision=1,
            expected_draft_revision=1,
            changes=[
                {
                    "change_type": "add_relation",
                    "object_key": "manual-join",
                    "source_id": "a",
                    "target_id": "b",
                    "enabled": True,
                }
            ],
        ),
    )
    manual = revisions.publish(
        "ds",
        DataLinkPublishRequest(
            expected_head=published.graph_version,
            expected_draft_revision=2,
            idempotency_key="manual",
        ),
    )
    candidate = create_candidate(repo, revisions, first)
    conflicts = history.list_conflicts("ds", candidate.graph_version)
    edge_key = next(c.object_key for c in conflicts.items if c.object_kind == "relation")
    resolved = history.resolve_candidate(
        "ds",
        DataLinkResolveCandidateRequest(
            candidate_graph_version=candidate.graph_version,
            expected_head=manual.graph_version,
            schema_revision=2,
            idempotency_key="resolve-manual",
            resolutions=[
                {"object_key": "a", "action": "discard"},
                {
                    "object_key": edge_key,
                    "action": "rebind",
                    "source_id": "new-a",
                    "target_id": "b",
                },
            ],
        ),
    )
    snapshot = repo.get_snapshot(repo.get_completed_build("ds", resolved.graph_version))
    edge = next(e for e in snapshot.edges if e.type == DataLinkEdgeType.JOINABLE)
    assert edge.confidence is None and edge.properties["enabled"] is True
    assert edge.properties["validation_status"] == "unverified"
    assert edge.source_id == "new-a" and edge.target_id == "b"


def test_version_diff_compares_snapshots_and_restore_previous_head(versions, monkeypatch):
    repo, revisions, history, first, published = versions
    initial = history.diff_version("ds", first.graph_version)
    assert initial.base_graph_version is None and initial.items == []
    curated = history.diff_version("ds", published.graph_version)
    assert curated.base_graph_version == first.graph_version
    updated = next(
        item for item in curated.items if item.kind == DataLinkVersionDiffKind.NODE_UPDATED
    )
    assert updated.object_key == "a"
    assert updated.automatic is not None and updated.automatic.description == "Automatic"
    assert updated.effective is not None and updated.effective.description == "Curated"
    revisions.save_draft(
        "ds",
        DataLinkDraftSaveRequest(
            base_graph_version=published.graph_version,
            schema_revision=1,
            expected_draft_revision=1,
            changes=[
                {
                    "change_type": "add_node",
                    "object_key": "manual-concept",
                    "node_type": "concept",
                    "name": "Revenue",
                },
                {
                    "change_type": "replace_mapping",
                    "object_key": "a",
                    "target_ids": ["manual-concept"],
                },
                {"change_type": "disable_relation", "object_key": "fk"},
            ],
        ),
    )
    mapped = revisions.publish(
        "ds",
        DataLinkPublishRequest(
            expected_head=published.graph_version,
            expected_draft_revision=2,
            idempotency_key="map",
        ),
    )
    mapped_diff = history.diff_version("ds", mapped.graph_version)
    kinds = {item.kind for item in mapped_diff.items}
    assert DataLinkVersionDiffKind.NODE_ADDED in kinds
    assert DataLinkVersionDiffKind.MAPPING_REPLACED in kinds
    assert DataLinkVersionDiffKind.RELATION_UPDATED in kinds
    mapping = next(
        item for item in mapped_diff.items if item.kind == DataLinkVersionDiffKind.MAPPING_REPLACED
    )
    assert mapping.after_targets == ["Revenue"]
    restored = history.restore(
        "ds",
        restore_request(
            first, published, expected_head=mapped.graph_version, idempotency_key="restore-diff"
        ),
    )
    restored_diff = history.diff_version("ds", restored.graph_version)
    assert restored_diff.base_graph_version == mapped.graph_version
    assert any(item.kind == DataLinkVersionDiffKind.NODE_REMOVED for item in restored_diff.items)
    monkeypatch.setattr("server.version_history._DIFF_LIMIT", 1)
    truncated = history.diff_version("ds", mapped.graph_version)
    assert truncated.truncated is True and len(truncated.items) == 1
    with pytest.raises(RevisionConflict) as missing:
        history.diff_version("ds", "missing")
    assert missing.value.code == DataLinkErrorCode.GRAPH_VERSION_NOT_FOUND
    with pytest.raises(RevisionConflict) as mismatch:
        history.diff_version("other", first.graph_version)
    assert mismatch.value.code == DataLinkErrorCode.DATASOURCE_MISMATCH
