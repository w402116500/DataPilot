"""Management routes permit safe candidate inspection without widening Agent access."""

import asyncio

import pytest
from contracts.datalink import DataLinkNodeType
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.models.graph import GraphEmbedding, GraphNode
from server.models.profile import ColumnProfile


@pytest.fixture
def management(tmp_path):
    settings = Settings(source_root=tmp_path / "sources", database_path=tmp_path / "graph.db")
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        repo = client.app.state.graph_repository
        nodes = tuple(
            GraphNode(
                f"n{i}",
                DataLinkNodeType.COLUMN,
                f"field{i}",
                "orders",
                properties={"source_ref": "private/source.csv"},
            )
            for i in range(3)
        )
        base = repo.claim_build("ds", 1).build
        base = repo.store_completed_graph(base.id, nodes, (), ())
        candidate = repo.claim_build("ds", 1).build
        repo.set_build_metadata(
            candidate.id,
            origin_kind="automated",
            publication_state="candidate",
            base_graph_version=base.graph_version,
        )
        profile = ColumnProfile(
            id="p",
            column_id="n0",
            column_name="field0",
            dtype="text",
            null_rate=0.1,
            distinct_count=2,
            unique_rate=0.5,
            sample_values=("sensitive-sample",),
            top_values=("sensitive-top",),
            min_value="sensitive-min",
            max_value="sensitive-max",
        )
        candidate = repo.store_completed_graph(
            candidate.id,
            nodes,
            (),
            (profile,),
            (GraphEmbedding("n0", "model-private", (0.123456,), "index-private"),),
        )
        yield client, base, candidate


def test_candidate_catalog_paginates_searches_and_excludes_raw_profile_values(management):
    client, _, candidate = management
    url = f"/v1/graphs/ds/versions/{candidate.graph_version}/catalog"
    response = client.get(url, params={"node_type": "column", "page": 2, "page_size": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert response.json()["items"][0]["node"]["id"] == "n1"
    filtered = client.get(url, params={"query": "field0"})
    assert filtered.status_code == 200 and filtered.json()["total"] == 1
    profile = filtered.json()["items"][0]["node"]["profile"]
    assert set(profile) == {"dtype", "semantic_type", "null_rate", "distinct_count", "unique_rate"}
    for forbidden in (
        "sensitive-",
        "source_ref",
        "private/source.csv",
        "sample_values",
        "top_values",
        "min_value",
        "max_value",
        "embedding",
        "index-private",
        "model-private",
    ):
        assert forbidden not in filtered.text


def test_candidate_management_route_does_not_make_it_agent_readable(management):
    client, base, candidate = management
    version = candidate.graph_version
    assert client.get(f"/v1/graphs/ds/versions/{version}/catalog").status_code == 200
    rejected = client.get("/v1/graphs/ds/catalog", params={"graph_version": version})
    assert (
        rejected.status_code == 404
        and rejected.json()["error"]["code"] == "GRAPH_VERSION_NOT_FOUND"
    )
    tool = asyncio.run(client.app.state.mcp_server.get_tool("datalink_explore"))
    with pytest.raises(ValueError, match="^GRAPH_VERSION_NOT_FOUND:"):
        tool.fn(datasource_id="ds", graph_version=version, query="field0")
    assert (
        client.app.state.graph_repository.get_head("ds").current_graph_version == base.graph_version
    )
    assert client.get(f"/v1/graphs/other/versions/{version}/catalog").status_code == 409
    assert client.get(f"/v1/graphs/ds/versions/{base.graph_version}/catalog").status_code == 409


def test_management_errors_are_structured_and_do_not_echo_inputs(management, monkeypatch):
    client, base, candidate = management
    stale = client.post(
        "/v1/graphs/ds/restore",
        json={
            "target_graph_version": base.graph_version,
            "expected_head": "private-stale-head",
            "schema_revision": 1,
            "idempotency_key": "restore",
        },
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "HEAD_STALE"
    assert "private-stale-head" not in stale.text
    invalid = client.get(
        f"/v1/graphs/ds/versions/{candidate.graph_version}/catalog", params={"page_size": 101}
    )
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    def fail(*args, **kwargs):
        raise RuntimeError("private-path-and-key")

    monkeypatch.setattr(client.app.state.version_history, "candidate_snapshot", fail)
    unexpected = client.get(f"/v1/graphs/ds/versions/{candidate.graph_version}/catalog")
    assert unexpected.status_code == 500 and unexpected.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "private-path-and-key" not in unexpected.text


def test_versions_and_restore_routes_use_wired_version_service(management):
    client, base, candidate = management
    history = client.get("/v1/graphs/ds/versions").json()
    assert history["total"] == 2
    assert (
        next(v for v in history["items"] if v["graph_version"] == candidate.graph_version)[
            "publication_state"
        ]
        == "candidate"
    )
    response = client.post(
        "/v1/graphs/ds/restore",
        json={
            "target_graph_version": base.graph_version,
            "expected_head": base.graph_version,
            "schema_revision": 1,
            "idempotency_key": "restore",
        },
    )
    assert response.status_code == 200 and response.json()["origin_kind"] == "restore"
    assert response.json()["graph_version"] != base.graph_version


def test_version_diff_route_is_management_readable_and_structured(management):
    client, base, candidate = management
    response = client.get(f"/v1/graphs/ds/versions/{base.graph_version}/diff")
    assert response.status_code == 200
    body = response.json()
    assert body["graph_version"] == base.graph_version
    assert body["items"] == [] and body["truncated"] is False
    candidate_diff = client.get(f"/v1/graphs/ds/versions/{candidate.graph_version}/diff")
    assert candidate_diff.status_code == 200
    assert candidate_diff.json()["base_graph_version"] == base.graph_version
    missing = client.get("/v1/graphs/ds/versions/missing/diff")
    assert (
        missing.status_code == 404 and missing.json()["error"]["code"] == "GRAPH_VERSION_NOT_FOUND"
    )
    other = client.get(f"/v1/graphs/other/versions/{base.graph_version}/diff")
    assert other.status_code == 409 and other.json()["error"]["code"] == "DATASOURCE_MISMATCH"
    for forbidden in (
        "sensitive-",
        "source_ref",
        "private/source.csv",
        "embedding",
        "index-private",
    ):
        assert forbidden not in response.text
        assert forbidden not in candidate_diff.text
