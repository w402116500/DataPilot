"""Published preview uses the real MCP adapter and the runtime's safe projection."""

from datetime import UTC, datetime

import httpx
import pytest
from agent_runtime.contracts import DataLinkExploreResponse
from agent_runtime.datalink_semantics import project_datalink_semantic_context
from application.agent_ports import DataLinkMcpPort
from application.datalink_client import DataLinkClient, DataLinkClientError
from application.datalink_preview import DataLinkPreviewService
from contracts.datalink import DataLinkExploreResult, DataLinkPreviewRequest
from contracts.datasources import DataSourceRead
from contracts.errors import AppError
from runtime.run_cancel_registry import RunCancellation


class SourceReader:
    def __init__(self):
        self.source = DataSourceRead(
            id="source",
            name="source",
            description=None,
            type="csv",
            status="ready",
            schema_revision=1,
            datalink_graph_version="version",
            datalink_build_id="build",
            last_error_code=None,
            last_error_message=None,
            last_test_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def get(self, datasource_id):
        assert datasource_id == self.source.id
        return self.source


@pytest.mark.asyncio
async def test_preview_matches_runtime_and_filters_sensitive_profile():
    raw = DataLinkExploreResult(
        datasource_id="source",
        graph_version="version",
        query="orders",
        nodes=[
            {
                "id": "internal-column",
                "type": "column",
                "name": "email",
                "table": "orders",
                "description": "Customer email",
                "profile": {
                    "column_id": "internal-column",
                    "dtype": "text",
                    "null_rate": 0,
                    "distinct_count": 1,
                    "unique_rate": 1,
                    "sample_values": ["private@example.test"],
                },
            }
        ],
        retrieval_mode="keyword",
        is_truncated=True,
    )
    calls = []

    async def call_tool(endpoint, arguments):
        calls.append(arguments)
        return raw.model_dump(mode="json")

    service = DataLinkPreviewService(
        SourceReader(),
        DataLinkMcpPort(
            endpoint="http://test/mcp",
            timeout_seconds=1,
            call_tool=call_tool,
        ),
    )
    payload = DataLinkPreviewRequest(graph_version="version", query=" orders ", max_nodes=5)
    result = await service.preview("source", payload, RunCancellation())
    expected = project_datalink_semantic_context(
        DataLinkExploreResponse(result=raw, cache_hit=False)
    )
    assert result.semantic_context == expected
    assert result.retrieval_mode == "keyword" and result.is_truncated
    serialized = result.model_dump_json()
    assert "internal-column" not in serialized and "private@example.test" not in serialized
    assert calls == [
        {"datasource_id": "source", "graph_version": "version", "query": "orders", "max_nodes": 5}
    ]
    with pytest.raises(AppError) as stale:
        await service.preview(
            "source", payload.model_copy(update={"graph_version": "old"}), RunCancellation()
        )
    assert stale.value.status_code == 409 and len(calls) == 1


@pytest.mark.asyncio
async def test_preview_failure_is_not_an_empty_success():
    async def call_tool(endpoint, arguments):
        raise TimeoutError

    reader = SourceReader()
    service = DataLinkPreviewService(
        reader,
        DataLinkMcpPort(
            endpoint="http://test/mcp",
            timeout_seconds=1,
            call_tool=call_tool,
        ),
    )
    payload = DataLinkPreviewRequest(graph_version="version", query="orders")
    with pytest.raises(AppError) as unavailable:
        await service.preview("source", payload, RunCancellation())
    assert unavailable.value.status_code == 503
    reader.source = reader.source.model_copy(update={"status": "deleted"})
    with pytest.raises(AppError) as deleted:
        await service.preview("source", payload, RunCancellation())
    assert deleted.value.status_code == 409


@pytest.mark.asyncio
async def test_catalog_client_rejects_wrong_version():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "datasource_id": "source",
                "graph_version": "wrong",
                "items": [],
                "page": 1,
                "page_size": 50,
                "total": 0,
            },
        )
    )
    client = DataLinkClient("http://test", timeout_seconds=1, transport=transport)
    with pytest.raises(DataLinkClientError) as error:
        await client.catalog("source", graph_version="version")
    assert error.value.code == "GRAPH_VERSION_NOT_FOUND"
