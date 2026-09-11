"""Catalog contracts must stay complete, versioned, and free of profile values."""

import json
from pathlib import Path

import pytest
from contracts.datalink import DataLinkEdgeEvidenceRead, DataLinkEdgeType, DataLinkNodeType
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.models.graph import GraphEdge, GraphNode, graph_provenance
from server.retrieval.catalog import _automatic_semantics


def _edge(
    edge_id: str,
    source_id: str,
    target_id: str,
    edge_type: DataLinkEdgeType,
    confidence: float,
) -> GraphEdge:
    return GraphEdge(
        id=edge_id,
        source_id=source_id,
        target_id=target_id,
        type=edge_type,
        confidence=confidence,
    )


def test_graph_provenance_keeps_recorded_mapping_and_unrecorded_unknown() -> None:
    recorded = GraphNode(
        id="concept",
        type=DataLinkNodeType.CONCEPT,
        name="获客渠道",
        properties={"provenance": "semantic_mapping"},
    )
    unrecorded = GraphNode(id="legacy", type=DataLinkNodeType.CONCEPT, name="获客渠道")
    column = GraphNode(
        id="channel", type=DataLinkNodeType.COLUMN, name="channel", table_name="customers"
    )
    inferred_kind = GraphEdge(
        id="rep",
        source_id="channel",
        target_id="concept",
        type=DataLinkEdgeType.REPRESENTS,
        confidence=0.9,
        evidence=DataLinkEdgeEvidenceRead(kind="semantic_mapping", summary="Mapped"),
    )
    recorded_edge = GraphEdge(
        id="has",
        source_id="entity",
        target_id="concept",
        type=DataLinkEdgeType.HAS_CONCEPT,
        confidence=0.8,
        properties={"provenance": "semantic_mapping"},
    )
    assert graph_provenance(recorded) == "semantic_mapping"
    assert graph_provenance(unrecorded) == "unknown"
    assert graph_provenance(column) == "structural"
    assert graph_provenance(inferred_kind) == "unknown"
    assert graph_provenance(recorded_edge) == "semantic_mapping"


def test_catalog_reads_recorded_semantic_mapping_provenance(tmp_path: Path) -> None:
    settings = Settings(source_root=tmp_path / "sources", database_path=tmp_path / "graph.db")
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.graph_repository
        build = repo.claim_build("source", 1).build
        nodes = [
            GraphNode(id="table", type=DataLinkNodeType.TABLE, name="customers"),
            GraphNode(
                id="channel",
                type=DataLinkNodeType.COLUMN,
                name="channel",
                table_name="customers",
            ),
            GraphNode(
                id="concept",
                type=DataLinkNodeType.CONCEPT,
                name="获客渠道",
                description="流量来源",
                properties={"provenance": "semantic_mapping"},
            ),
            GraphNode(
                id="entity",
                type=DataLinkNodeType.ENTITY,
                name="客户",
                properties={"provenance": "semantic_mapping"},
            ),
        ]
        edges = [
            GraphEdge(
                id="contains",
                source_id="table",
                target_id="channel",
                type=DataLinkEdgeType.CONTAINS,
                confidence=1,
            ),
            GraphEdge(
                id="rep",
                source_id="channel",
                target_id="concept",
                type=DataLinkEdgeType.REPRESENTS,
                confidence=0.9,
                properties={"provenance": "semantic_mapping"},
            ),
            GraphEdge(
                id="has",
                source_id="entity",
                target_id="concept",
                type=DataLinkEdgeType.HAS_CONCEPT,
                confidence=0.8,
                properties={"provenance": "semantic_mapping"},
            ),
        ]
        version = repo.store_completed_graph(build.id, tuple(nodes), tuple(edges), ()).graph_version
        listed = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "column", "page_size": 50},
        ).json()
        item = listed["items"][0]
        assert item["node"]["id"] == "channel"
        assert item["provenance"] == "structural"
        assert item["primary_mapping"] == {
            "concept_name": "获客渠道",
            "concept_description": "流量来源",
            "entity_name": "客户",
            "field_to_concept_confidence": 0.9,
            "provenance": "semantic_mapping",
        }
        concepts = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "concept", "page_size": 50},
        ).json()
        assert concepts["items"][0]["provenance"] == "semantic_mapping"
        entities = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "entity", "page_size": 50},
        ).json()
        assert entities["items"][0]["provenance"] == "semantic_mapping"
        relations = client.get(
            "/v1/graphs/source/relations",
            params={"graph_version": version, "node_id": "channel"},
        ).json()
        assert relations["items"][0]["id"] == "rep"
        assert relations["items"][0]["provenance"] == "semantic_mapping"


def test_catalog_exact_mappings_pagination_relations_and_version_scope(tmp_path: Path) -> None:
    settings = Settings(source_root=tmp_path / "sources", database_path=tmp_path / "graph.db")
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.graph_repository
        build = repo.claim_build("source", 1).build
        nodes = [GraphNode(id="table", type=DataLinkNodeType.TABLE, name="orders")]
        nodes += [
            GraphNode(
                id=f"column{i}",
                type=DataLinkNodeType.COLUMN,
                name=f"field{i:03}",
                table_name="orders",
            )
            for i in range(120)
        ]
        nodes += [
            GraphNode(id="concept1", type=DataLinkNodeType.CONCEPT, name="amount"),
            GraphNode(id="concept2", type=DataLinkNodeType.CONCEPT, name="amount"),
            GraphNode(id="entity1", type=DataLinkNodeType.ENTITY, name="Order"),
            GraphNode(id="entity2", type=DataLinkNodeType.ENTITY, name="Payment"),
        ]
        edges = [
            GraphEdge(
                id=f"contains{i}",
                source_id="table",
                target_id=f"column{i}",
                type=DataLinkEdgeType.CONTAINS,
                confidence=1,
            )
            for i in range(120)
        ]
        edges += [
            GraphEdge(
                id="rep1",
                source_id="column0",
                target_id="concept1",
                type=DataLinkEdgeType.REPRESENTS,
                confidence=0.8,
            ),
            GraphEdge(
                id="rep2",
                source_id="column1",
                target_id="concept2",
                type=DataLinkEdgeType.REPRESENTS,
                confidence=0.7,
            ),
            GraphEdge(
                id="has1",
                source_id="entity1",
                target_id="concept1",
                type=DataLinkEdgeType.HAS_CONCEPT,
                confidence=0.9,
            ),
            GraphEdge(
                id="has2",
                source_id="entity2",
                target_id="concept2",
                type=DataLinkEdgeType.HAS_CONCEPT,
                confidence=0.6,
            ),
            GraphEdge(
                id="wrong",
                source_id="column0",
                target_id="concept2",
                type=DataLinkEdgeType.CORRELATED,
                confidence=0.5,
            ),
            GraphEdge(
                id="join",
                source_id="column0",
                target_id="column1",
                type=DataLinkEdgeType.JOINABLE,
                confidence=0.65,
            ),
        ]
        version = repo.store_completed_graph(build.id, tuple(nodes), tuple(edges), ()).graph_version
        params = {"graph_version": version, "type": "column", "page_size": 50, "page": 3}
        response = client.get("/v1/graphs/source/catalog", params=params)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 120 and len(data["items"]) == 20
        assert data["items"][0]["node"]["name"] == "field100"
        detail = client.get(
            "/v1/graphs/source/catalog/column0", params={"graph_version": version}
        ).json()
        assert detail["total"] == 1
        assert detail["mappings"][0]["concept"]["id"] == "concept1"
        assert detail["mappings"][0]["entity"]["id"] == "entity1"
        assert detail["item"]["provenance"] == "structural"
        relations = client.get(
            "/v1/graphs/source/relations", params={"graph_version": version, "node_id": "column0"}
        ).json()
        by_id = {r["id"]: r for r in relations["items"]}
        assert by_id["join"]["join_eligible"] is True
        relation = client.get("/v1/graphs/source/relations/join", params={"graph_version": version})
        assert relation.status_code == 200
        assert relation.json()["item"]["id"] == "join"
        assert (
            client.get(
                "/v1/graphs/source/relations/missing", params={"graph_version": version}
            ).status_code
            == 409
        )
        assert by_id["wrong"]["join_eligible"] is False
        assert by_id["wrong"]["provenance"] == "unknown"
        assert client.get("/v1/graphs/other/catalog", params=params).status_code == 409
        assert (
            client.get("/v1/graphs/source/catalog", params={"graph_version": "missing"}).status_code
            == 404
        )
        assert (
            client.get("/v1/graphs/source/catalog", params={**params, "page": 0}).status_code == 422
        )
        empty = client.get("/v1/graphs/source/catalog", params={**params, "query": "none"}).json()
        assert empty["items"] == [] and empty["total"] == 0
        first_page = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "column", "page_size": 50, "page": 1},
        ).json()
        column0 = next(item for item in first_page["items"] if item["node"]["id"] == "column0")
        assert column0["primary_mapping"] == {
            "concept_name": "amount",
            "concept_description": None,
            "entity_name": "Order",
            "field_to_concept_confidence": 0.8,
            "provenance": "unknown",
        }
        concepts = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "concept", "page_size": 50, "page": 1},
        ).json()
        assert concepts["total"] == 2
        assert all(item["primary_mapping"] is None for item in concepts["items"])
        by_concept = {item["node"]["id"]: item for item in concepts["items"]}
        assert by_concept["concept1"]["mapped_columns"] == [{"table": "orders", "name": "field000"}]
        assert by_concept["concept2"]["mapped_columns"] == [{"table": "orders", "name": "field001"}]
        assert column0["mapped_columns"] == []


def test_catalog_primary_mapping_ignores_disabled_edges_and_indexes_concept_text(
    tmp_path: Path,
) -> None:
    settings = Settings(source_root=tmp_path / "sources", database_path=tmp_path / "graph.db")
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.graph_repository
        build = repo.claim_build("source", 1).build
        nodes = [
            GraphNode(id="table", type=DataLinkNodeType.TABLE, name="orders"),
            GraphNode(
                id="priced",
                type=DataLinkNodeType.COLUMN,
                name="amount",
                table_name="orders",
            ),
            GraphNode(
                id="plain",
                type=DataLinkNodeType.COLUMN,
                name="notes",
                table_name="orders",
            ),
            GraphNode(
                id="concept_high",
                type=DataLinkNodeType.CONCEPT,
                name="成交额",
                description="扣除优惠前的订单金额",
                aliases=("gross_amount",),
            ),
            GraphNode(
                id="concept_low",
                type=DataLinkNodeType.CONCEPT,
                name="标价",
                description="应被更低置信映射取代",
            ),
            GraphNode(
                id="concept_off",
                type=DataLinkNodeType.CONCEPT,
                name="停用概念",
                description="禁用映射不得出现",
            ),
            GraphNode(
                id="entity_order",
                type=DataLinkNodeType.ENTITY,
                name="订单",
                description="一次成交记录",
            ),
        ]
        edges = [
            GraphEdge(
                id="contains_priced",
                source_id="table",
                target_id="priced",
                type=DataLinkEdgeType.CONTAINS,
                confidence=1,
            ),
            GraphEdge(
                id="contains_plain",
                source_id="table",
                target_id="plain",
                type=DataLinkEdgeType.CONTAINS,
                confidence=1,
            ),
            GraphEdge(
                id="rep_high",
                source_id="priced",
                target_id="concept_high",
                type=DataLinkEdgeType.REPRESENTS,
                confidence=0.91,
            ),
            GraphEdge(
                id="rep_low",
                source_id="priced",
                target_id="concept_low",
                type=DataLinkEdgeType.REPRESENTS,
                confidence=0.4,
            ),
            GraphEdge(
                id="rep_off",
                source_id="priced",
                target_id="concept_off",
                type=DataLinkEdgeType.REPRESENTS,
                confidence=0.99,
                properties={"enabled": False},
            ),
            GraphEdge(
                id="has_high",
                source_id="entity_order",
                target_id="concept_high",
                type=DataLinkEdgeType.HAS_CONCEPT,
                confidence=0.7,
            ),
        ]
        version = repo.store_completed_graph(build.id, tuple(nodes), tuple(edges), ()).graph_version
        listed = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "column", "page_size": 50},
        ).json()
        by_id = {item["node"]["id"]: item for item in listed["items"]}
        assert by_id["priced"]["primary_mapping"] == {
            "concept_name": "成交额",
            "concept_description": "扣除优惠前的订单金额",
            "entity_name": "订单",
            "field_to_concept_confidence": 0.91,
            "provenance": "unknown",
        }
        assert by_id["plain"]["primary_mapping"] is None
        found = client.get(
            "/v1/graphs/source/catalog",
            params={
                "graph_version": version,
                "type": "column",
                "query": "gross_amount",
                "page_size": 50,
            },
        ).json()
        assert [item["node"]["id"] for item in found["items"]] == ["priced"]
        missed = client.get(
            "/v1/graphs/source/catalog",
            params={
                "graph_version": version,
                "type": "column",
                "query": "停用概念",
                "page_size": 50,
            },
        ).json()
        assert missed["items"] == [] and missed["total"] == 0
        concepts = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "concept", "page_size": 50},
        ).json()
        by_concept = {item["node"]["id"]: item for item in concepts["items"]}
        assert by_concept["concept_high"]["mapped_columns"] == [
            {"table": "orders", "name": "amount"}
        ]
        assert by_concept["concept_off"]["mapped_columns"] == []
        entities = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "entity", "page_size": 50},
        ).json()
        assert entities["items"][0]["mapped_columns"] == [{"table": "orders", "name": "amount"}]


def test_catalog_mapped_columns_stay_unique_across_cartesian_entities(tmp_path: Path) -> None:
    settings = Settings(source_root=tmp_path / "sources", database_path=tmp_path / "graph.db")
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.graph_repository
        build = repo.claim_build("source", 1).build
        nodes = [
            GraphNode(id="table", type=DataLinkNodeType.TABLE, name="customers"),
            GraphNode(
                id="col_customers",
                type=DataLinkNodeType.COLUMN,
                name="customer_id",
                table_name="customers",
            ),
            GraphNode(
                id="col_orders",
                type=DataLinkNodeType.COLUMN,
                name="customer_id",
                table_name="orders",
            ),
            GraphNode(id="concept", type=DataLinkNodeType.CONCEPT, name="客户标识"),
            GraphNode(id="entity_customer", type=DataLinkNodeType.ENTITY, name="客户"),
            GraphNode(id="entity_order", type=DataLinkNodeType.ENTITY, name="订单"),
        ]
        edges = [
            GraphEdge(
                id="contains_customers",
                source_id="table",
                target_id="col_customers",
                type=DataLinkEdgeType.CONTAINS,
                confidence=1,
            ),
            GraphEdge(
                id="rep_customers",
                source_id="col_customers",
                target_id="concept",
                type=DataLinkEdgeType.REPRESENTS,
                confidence=0.95,
            ),
            GraphEdge(
                id="rep_orders",
                source_id="col_orders",
                target_id="concept",
                type=DataLinkEdgeType.REPRESENTS,
                confidence=0.9,
            ),
            GraphEdge(
                id="has_customer",
                source_id="entity_customer",
                target_id="concept",
                type=DataLinkEdgeType.HAS_CONCEPT,
                confidence=0.95,
            ),
            GraphEdge(
                id="has_order",
                source_id="entity_order",
                target_id="concept",
                type=DataLinkEdgeType.HAS_CONCEPT,
                confidence=0.9,
            ),
        ]
        version = repo.store_completed_graph(build.id, tuple(nodes), tuple(edges), ()).graph_version
        listed = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "concept", "page_size": 50},
        ).json()
        assert listed["items"][0]["mapping_count"] == 4
        assert listed["items"][0]["mapped_columns"] == [
            {"table": "customers", "name": "customer_id"},
            {"table": "orders", "name": "customer_id"},
        ]
        detail = client.get(
            "/v1/graphs/source/catalog/concept", params={"graph_version": version}
        ).json()
        assert detail["total"] == 4
        entities = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "entity", "page_size": 50},
        ).json()
        by_entity = {item["node"]["id"]: item for item in entities["items"]}
        expected = listed["items"][0]["mapped_columns"]
        assert by_entity["entity_customer"]["mapped_columns"] == expected
        assert by_entity["entity_order"]["mapped_columns"] == expected


def test_catalog_keeps_entity_with_tightest_same_table_concept_overlap(tmp_path: Path) -> None:
    settings = Settings(source_root=tmp_path / "sources", database_path=tmp_path / "graph.db")
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.graph_repository
        build = repo.claim_build("source", 1).build
        nodes = [
            GraphNode(id="home_table", type=DataLinkNodeType.TABLE, name="home"),
            GraphNode(id="ref_table", type=DataLinkNodeType.TABLE, name="ref"),
            GraphNode(
                id="home_anchor",
                type=DataLinkNodeType.COLUMN,
                name="anchor",
                table_name="home",
            ),
            GraphNode(
                id="home_title",
                type=DataLinkNodeType.COLUMN,
                name="title",
                table_name="home",
            ),
            GraphNode(
                id="ref_anchor",
                type=DataLinkNodeType.COLUMN,
                name="anchor",
                table_name="ref",
            ),
            GraphNode(
                id="ref_measure",
                type=DataLinkNodeType.COLUMN,
                name="measure",
                table_name="ref",
            ),
            GraphNode(id="concept_key", type=DataLinkNodeType.CONCEPT, name="shared_key"),
            GraphNode(id="concept_title", type=DataLinkNodeType.CONCEPT, name="home_title"),
            GraphNode(id="concept_measure", type=DataLinkNodeType.CONCEPT, name="ref_measure"),
            GraphNode(id="entity_home", type=DataLinkNodeType.ENTITY, name="home_record"),
            GraphNode(id="entity_ref", type=DataLinkNodeType.ENTITY, name="ref_record"),
        ]
        contains = DataLinkEdgeType.CONTAINS
        represents = DataLinkEdgeType.REPRESENTS
        has_concept = DataLinkEdgeType.HAS_CONCEPT
        edges = [
            _edge("contains_home_anchor", "home_table", "home_anchor", contains, 1),
            _edge("contains_home_title", "home_table", "home_title", contains, 1),
            _edge("contains_ref_anchor", "ref_table", "ref_anchor", contains, 1),
            _edge("contains_ref_measure", "ref_table", "ref_measure", contains, 1),
            _edge("rep_home_anchor", "home_anchor", "concept_key", represents, 0.95),
            _edge("rep_home_title", "home_title", "concept_title", represents, 0.95),
            _edge("rep_ref_anchor", "ref_anchor", "concept_key", represents, 0.95),
            _edge("rep_ref_measure", "ref_measure", "concept_measure", represents, 0.95),
            _edge("has_home_key", "entity_home", "concept_key", has_concept, 0.95),
            _edge("has_home_title", "entity_home", "concept_title", has_concept, 0.95),
            _edge("has_ref_key", "entity_ref", "concept_key", has_concept, 0.95),
            _edge("has_ref_measure", "entity_ref", "concept_measure", has_concept, 0.95),
        ]
        version = repo.store_completed_graph(build.id, tuple(nodes), tuple(edges), ()).graph_version
        listed = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "column", "page_size": 50},
        ).json()
        by_id = {item["node"]["id"]: item for item in listed["items"]}
        assert by_id["home_anchor"]["primary_mapping"]["entity_name"] == "home_record"
        assert by_id["ref_anchor"]["primary_mapping"]["entity_name"] == "ref_record"
        home_detail = client.get(
            "/v1/graphs/source/catalog/home_anchor", params={"graph_version": version}
        ).json()
        assert {row["entity"]["id"] for row in home_detail["mappings"]} == {"entity_home"}
        ref_detail = client.get(
            "/v1/graphs/source/catalog/ref_anchor", params={"graph_version": version}
        ).json()
        assert {row["entity"]["id"] for row in ref_detail["mappings"]} == {"entity_ref"}
        concept = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "concept", "page_size": 50},
        ).json()
        by_concept = {item["node"]["id"]: item for item in concept["items"]}
        assert by_concept["concept_key"]["mapped_columns"] == [
            {"table": "home", "name": "anchor"},
            {"table": "ref", "name": "anchor"},
        ]
        entities = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "entity", "page_size": 50},
        ).json()
        by_entity = {item["node"]["id"]: item for item in entities["items"]}
        assert by_entity["entity_home"]["mapped_columns"] == [
            {"table": "home", "name": "anchor"},
            {"table": "home", "name": "title"},
        ]
        assert by_entity["entity_ref"]["mapped_columns"] == [
            {"table": "ref", "name": "anchor"},
            {"table": "ref", "name": "measure"},
        ]


def test_catalog_keeps_tied_entities_when_table_overlap_is_equal(tmp_path: Path) -> None:
    settings = Settings(source_root=tmp_path / "sources", database_path=tmp_path / "graph.db")
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.graph_repository
        build = repo.claim_build("source", 1).build
        nodes = [
            GraphNode(
                id="shared_col",
                type=DataLinkNodeType.COLUMN,
                name="code",
                table_name="items",
            ),
            GraphNode(id="concept", type=DataLinkNodeType.CONCEPT, name="shared_key"),
            GraphNode(id="entity_a", type=DataLinkNodeType.ENTITY, name="record_a"),
            GraphNode(id="entity_b", type=DataLinkNodeType.ENTITY, name="record_b"),
        ]
        edges = [
            _edge("rep", "shared_col", "concept", DataLinkEdgeType.REPRESENTS, 0.9),
            _edge("has_a", "entity_a", "concept", DataLinkEdgeType.HAS_CONCEPT, 0.8),
            _edge("has_b", "entity_b", "concept", DataLinkEdgeType.HAS_CONCEPT, 0.6),
        ]
        version = repo.store_completed_graph(build.id, tuple(nodes), tuple(edges), ()).graph_version
        listed = client.get(
            "/v1/graphs/source/catalog",
            params={"graph_version": version, "type": "column", "page_size": 50},
        ).json()
        assert listed["items"][0]["primary_mapping"]["entity_name"] == "record_a"
        detail = client.get(
            "/v1/graphs/source/catalog/shared_col", params={"graph_version": version}
        ).json()
        assert {row["entity"]["id"] for row in detail["mappings"]} == {"entity_a", "entity_b"}


def test_catalog_projects_automatic_semantics_and_rejects_corrupt_override():
    node = GraphNode(
        "a",
        DataLinkNodeType.COLUMN,
        "amount",
        "orders",
        description="人工说明",
        properties={
            "semantic_override": json.dumps(
                {
                    "description": {"automatic": "自动说明", "value": "人工说明"},
                    "aliases": {"automatic": ["实付"], "value": ["成交额"]},
                }
            )
        },
    )
    automatic = _automatic_semantics(node)
    assert automatic is not None
    assert automatic.description == "自动说明" and automatic.aliases == ["实付"]
    with pytest.raises(ValueError, match="object"):
        _automatic_semantics(
            GraphNode(
                "b",
                DataLinkNodeType.COLUMN,
                "id",
                "orders",
                properties={"semantic_override": "[1]"},
            )
        )
    with pytest.raises(ValueError, match="aliases"):
        _automatic_semantics(
            GraphNode(
                "c",
                DataLinkNodeType.COLUMN,
                "id",
                "orders",
                properties={"semantic_override": json.dumps({"aliases": {"automatic": "bad"}})},
            )
        )
