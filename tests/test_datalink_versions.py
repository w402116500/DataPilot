"""Version operations validate identity and never repeat an uncertain write."""

from types import SimpleNamespace

import httpx
import pytest
from application.datalink_client import DataLinkClient, DataLinkClientError
from application.datalink_versions import DataLinkVersionsService
from contracts.datalink import DataLinkResolveCandidateRequest, DataLinkRestoreRequest
from contracts.errors import AppError
from fastapi import FastAPI
from server.dependencies import get_datalink_versions_service
from server.routes.datasources import router


def publication(**changes):
    return dict(
        datasource_id="source",
        graph_version="new",
        previous_graph_version="head",
        source_graph_version="old",
        origin_kind="restore",
        idempotency_key="key",
        **changes,
    )


def restore_request(**changes):
    values = dict(
        target_graph_version="old", expected_head="head", schema_revision=2, idempotency_key="key"
    )
    return DataLinkRestoreRequest(**(values | changes))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("datasource_id", "other"),
        ("source_graph_version", "wrong"),
        ("previous_graph_version", "wrong"),
        ("idempotency_key", "wrong"),
        ("origin_kind", "manual"),
        ("graph_version", "head"),
    ],
)
async def test_restore_rejects_mismatched_publication_identity(field, value):
    payload = publication()
    payload[field] = value
    client = DataLinkClient(
        "http://datalink",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(DataLinkClientError) as error:
        await client.restore("source", restore_request())
    assert error.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["restore", "resolve_candidate"])
async def test_version_writes_do_not_retry_timeout(action):
    requests = []

    def transport(request):
        requests.append(request)
        raise httpx.ReadTimeout("unknown outcome")

    client = DataLinkClient(
        "http://datalink", timeout_seconds=1, transport=httpx.MockTransport(transport)
    )
    payload = (
        restore_request()
        if action == "restore"
        else DataLinkResolveCandidateRequest(
            candidate_graph_version="candidate",
            expected_head="head",
            schema_revision=2,
            idempotency_key="key",
            resolutions=[dict(object_key="node", action="discard")],
        )
    )
    with pytest.raises(DataLinkClientError):
        await getattr(client, action)("source", payload)
    assert len(requests) == 1


class Sources:
    def __init__(self):
        self.schema_revision = 2
        self.status_reads = []
        self.deleted = False

    async def get_readable(self, datasource_id):
        if self.deleted:
            raise AppError("DATASOURCE_NOT_READY", "deleted", status_code=409)
        return SimpleNamespace(id=datasource_id, schema_revision=self.schema_revision)

    async def datalink_status(self, datasource_id):
        self.status_reads.append(datasource_id)
        return SimpleNamespace(current_graph_version="actual-head")


@pytest.mark.asyncio
async def test_restore_checks_schema_before_write_and_reconciles_actual_head():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json=publication())

    sources = Sources()
    client = DataLinkClient(
        "http://datalink", timeout_seconds=1, transport=httpx.MockTransport(transport)
    )
    service = DataLinkVersionsService(sources, client)
    with pytest.raises(AppError):
        await service.restore("source", restore_request(schema_revision=1))
    assert not requests
    result = await service.restore("source", restore_request())
    assert result.graph_version == "new" and sources.status_reads == ["source"]
    assert requests[0].url.path == "/v1/graphs/source/restore"
    sources.deleted = True
    with pytest.raises(AppError):
        await service.restore("source", restore_request())
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_candidate_catalog_rejects_schema_before_loading_rebinding_options():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=dict(
                datasource_id="source",
                candidate_graph_version="candidate",
                base_graph_version="head",
                schema_revision=1,
                items=[],
            ),
        )

    service = DataLinkVersionsService(
        Sources(),
        DataLinkClient(
            "http://datalink", timeout_seconds=1, transport=httpx.MockTransport(transport)
        ),
    )
    with pytest.raises(AppError):
        await service.catalog("source", "candidate", node_type="column", page=1, page_size=100)
    assert len(requests) == 1 and requests[0].url.path.endswith("/conflicts")


@pytest.mark.asyncio
async def test_version_read_identity_and_catalog_query_contract():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=dict(
                datasource_id="source",
                graph_version="candidate",
                items=[],
                page=1,
                page_size=100,
                total=0,
            ),
        )

    client = DataLinkClient(
        "http://datalink", timeout_seconds=1, transport=httpx.MockTransport(transport)
    )
    result = await client.version_catalog(
        "source", "candidate", node_type="column", page=1, page_size=100
    )
    assert result.graph_version == "candidate"
    assert requests[0].url.params["node_type"] == "column"
    with pytest.raises(DataLinkClientError):
        await client.version_catalog("source", "different", node_type=None, page=1, page_size=100)


@pytest.mark.asyncio
async def test_version_diff_identity_contract():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=dict(
                datasource_id="source",
                graph_version="candidate",
                base_graph_version="head",
                origin_kind="manual",
                publication_state="published",
                items=[],
                truncated=False,
            ),
        )

    client = DataLinkClient(
        "http://datalink", timeout_seconds=1, transport=httpx.MockTransport(transport)
    )
    result = await client.version_diff("source", "candidate")
    assert result.graph_version == "candidate" and result.items == []
    assert str(requests[0].url).endswith("/versions/candidate/diff")
    with pytest.raises(DataLinkClientError):
        await client.version_diff("source", "different")


@pytest.mark.asyncio
async def test_registered_version_routes_preserve_shared_envelopes_and_publication_reconciliation():
    def transport(request):
        path = request.url.path
        if path.endswith("/versions"):
            result = dict(datasource_id="source", items=[], page=1, page_size=30, total=0)
        elif path.endswith("/conflicts"):
            result = dict(
                datasource_id="source",
                candidate_graph_version="candidate",
                base_graph_version="head",
                schema_revision=2,
                items=[],
            )
        elif path.endswith("/catalog"):
            result = dict(
                datasource_id="source",
                graph_version="candidate",
                items=[],
                page=1,
                page_size=100,
                total=0,
            )
        elif path.endswith("/diff"):
            result = dict(
                datasource_id="source",
                graph_version="candidate",
                base_graph_version="head",
                origin_kind="automated",
                publication_state="candidate",
                items=[],
                truncated=False,
            )
        else:
            result = publication()
            if path.endswith("/resolve-candidate"):
                result.update(source_graph_version="candidate", origin_kind="manual")
        return httpx.Response(200, json=result)

    sources = Sources()
    upstream = DataLinkClient(
        "http://datalink", timeout_seconds=1, transport=httpx.MockTransport(transport)
    )
    service = DataLinkVersionsService(sources, upstream)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_datalink_versions_service] = lambda: service
    prefix = "/datasources/source/datalink"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://main"
    ) as client:
        for suffix in (
            "/versions",
            "/versions/candidate/conflicts",
            "/versions/candidate/catalog",
            "/versions/candidate/diff",
        ):
            response = await client.get(prefix + suffix)
            assert response.status_code == 200, response.text
            assert response.json()["data"]["datasource_id"] == "source"
            assert response.json()["error"] is None and response.json()["request_id"]
        response = await client.post(prefix + "/restore", json=restore_request().model_dump())
        assert response.status_code == 200 and response.json()["data"]["origin_kind"] == "restore"
        payload = dict(
            candidate_graph_version="candidate",
            expected_head="head",
            schema_revision=2,
            idempotency_key="key",
            resolutions=[dict(object_key="node", action="discard")],
        )
        response = await client.post(prefix + "/resolve-candidate", json=payload)
        assert response.status_code == 200 and response.json()["data"]["origin_kind"] == "manual"
        invalid = await client.get(prefix + "/versions?page_size=101")
        assert invalid.status_code == 422
    assert sources.status_reads == ["source", "source"]
