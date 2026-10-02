"""Manual revisions retain admin facts while Agent paths use enabled edges only."""

from dataclasses import replace

import pytest
from contracts.datalink import (
    DataLinkDraftChange,
    DataLinkEdgeType,
    DataLinkExploreRequest,
    DataLinkNodeType,
)
from pydantic import ValidationError

from server.graph.repository import GraphRepository
from server.graph.storage import GraphStorage
from server.models.graph import GraphEdge, GraphNode
from server.retrieval.explore import GraphAccessError, GraphExplorer, _bfs_join_paths


def test_manual_paths_and_mappings_are_unscored_and_disabled_facts_remain_visible(tmp_path):
    repo = GraphRepository(GraphStorage(tmp_path / "graph.db"))
    repo.initialize()
    build = repo.claim_build("source", 1).build
    nodes = tuple(
        GraphNode(id=name, type="column", name="id", table_name=name)
        for name in ("orders", "customers", "payments")
    ) + (
        GraphNode(
            id="concept", type="concept", name="customer", properties={"provenance": "manual"}
        ),
    )
    edges = (
        GraphEdge("auto", "orders", "payments", "joinable", 0.5),
        GraphEdge(
            "manual",
            "orders",
            "customers",
            "joinable",
            None,
            properties={"provenance": "manual", "enabled": True},
        ),
        GraphEdge(
            "disabled", "customers", "payments", "foreign_key", 1, properties={"enabled": False}
        ),
        GraphEdge(
            "mapping",
            "orders",
            "concept",
            "represents",
            None,
            properties={"provenance": "manual", "enabled": True},
        ),
    )
    # Internal models normally receive enums from the compiler and repository.
    from contracts.datalink import DataLinkEdgeType, DataLinkNodeType

    nodes = tuple(replace(node, type=DataLinkNodeType(node.type)) for node in nodes)
    edges = tuple(replace(edge, type=DataLinkEdgeType(edge.type)) for edge in edges)
    build = repo.store_completed_graph(build.id, nodes, edges, ())
    explorer = GraphExplorer(repo)
    request = DataLinkExploreRequest(
        datasource_id="source", graph_version=build.graph_version, query="id", max_nodes=12
    )
    result = explorer.explore(request)
    assert not any(edge.type == "foreign_key" for edge in result.edges)
    assert result.join_paths[0].confidence == 0.5
    manual_paths = [path for path in result.join_paths if path.provenance == "manual"]
    assert manual_paths and all(path.confidence is None for path in manual_paths)
    assert all("unverified" in path.evidence for path in manual_paths)
    assert any("no statistical confidence" in warning for warning in result.warnings)
    catalog = explorer.catalog("source", build.graph_version)
    disabled = next(item for item in catalog.relations if item.id == "disabled")
    assert not disabled.enabled and not disabled.join_eligible
    assert any(
        not edge.enabled for edge in explorer.read_graph("source", build.graph_version).edges
    )
    assert catalog.mappings[0].field_to_concept_confidence is None
    assert catalog.mappings[0].field_to_concept_provenance == "manual"
    recall = explorer.explore(request.model_copy(update={"query": "customer"}))
    assert any(node.id == "orders" for node in recall.nodes)

    snapshot = repo.get_snapshot(build)
    pending = replace(edges[1], properties={"provenance": "manual"})
    preview = explorer.explore_snapshot(replace(snapshot, edges=(pending,)), request)
    assert not preview.edges and not preview.join_paths
    assert preview.retrieval_mode == "keyword"


def test_candidate_version_cannot_be_explored(tmp_path):
    repo = GraphRepository(GraphStorage(tmp_path / "graph.db"))
    repo.initialize()
    build = repo.claim_build("source", 1).build
    repo.set_build_metadata(
        build.id, origin_kind="manual", publication_state="candidate", base_graph_version=None
    )
    build = repo.store_completed_graph(
        build.id, (GraphNode("table", DataLinkNodeType.TABLE, "orders"),), (), ()
    )
    with pytest.raises(GraphAccessError):
        GraphExplorer(repo).explore(
            DataLinkExploreRequest(
                datasource_id="source", graph_version=build.graph_version, query="id"
            )
        )


def test_typed_manual_changes_disallow_fake_foreign_keys_and_default_disabled():
    base = {
        "change_type": "add_relation",
        "object_key": "manual",
        "source_id": "a",
        "target_id": "b",
    }
    assert DataLinkDraftChange(**base).enabled is False
    change = DataLinkDraftChange(**base)
    assert DataLinkDraftChange.model_validate_json(change.model_dump_json()) == change
    with pytest.raises(ValidationError):
        DataLinkDraftChange(**base, relation_type="foreign_key")
    with pytest.raises(ValidationError):
        DataLinkDraftChange(change_type="update_node", object_key="a", source_id="b")
    with pytest.raises(ValidationError):
        DataLinkDraftChange(change_type="update_node", object_key="a", name="   ")
    assert DataLinkDraftChange(change_type="update_node", object_key="a", aliases=[]).aliases == []


def test_path_limit_cannot_hide_longer_foreign_key_path_behind_direct_candidates():
    nodes = {name: GraphNode(name, DataLinkNodeType.COLUMN, "id", name) for name in ("a", "b", "c")}
    edges = (
        GraphEdge("direct", "a", "c", DataLinkEdgeType.JOINABLE, 0.99),
        GraphEdge("fk1", "a", "b", DataLinkEdgeType.FOREIGN_KEY, 1),
        GraphEdge("fk2", "b", "c", DataLinkEdgeType.FOREIGN_KEY, 1),
    )
    paths = _bfs_join_paths("a", "c", edges, nodes, max_paths=1)
    assert len(paths) == 1 and paths[0].tables == ["a", "b", "c"]
    assert paths[0].provenance == "database_foreign_key"
