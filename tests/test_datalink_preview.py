"""Published preview uses the real MCP adapter and the runtime's safe projection."""

from datetime import UTC, datetime

import httpx
import pytest
from agent_runtime.contracts import (
    AgentFailure,
    DataLinkExploreCommand,
    DataLinkExploreResponse,
)
from agent_runtime.datalink_semantics import project_datalink_semantic_context
from application.agent_ports import DataLinkMcpPort
from application.datalink_client import DataLinkClient, DataLinkClientError
from application.datalink_preview import DataLinkPreviewService
from contracts.datalink import DataLinkExploreResult, DataLinkPreviewRequest
from contracts.datasources import DataSourceRead
from contracts.errors import AppError
from contracts.sensitive_fields import SensitiveFieldPolicy
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


def _profile_node(node_id: str, table: str, name: str, typical_values: list[str]) -> dict:
    return {
        "id": node_id,
        "type": "column",
        "table": table,
        "name": name,
        "profile": {
            "column_id": node_id,
            "dtype": "text",
            "null_rate": 0,
            "distinct_count": len(typical_values),
            "unique_rate": 1,
            "typical_values": typical_values,
        },
    }


@pytest.mark.asyncio
async def test_mcp_port_strips_masked_typical_values_before_entering_backend():
    """绑定了遮蔽清单的 MCP 端口在结果进入主后端前剥掉敏感列典型取值。"""

    raw = DataLinkExploreResult(
        datasource_id="source",
        graph_version="version",
        query="orders",
        nodes=[
            _profile_node("a", "orders", "email", ["a@example.test", "b@example.test"]),
            _profile_node("b", "orders", "status", ["paid", "pending"]),
        ],
        retrieval_mode="keyword",
        is_truncated=False,
    )

    async def call_tool(endpoint, arguments):
        return raw.model_dump(mode="json")

    port = DataLinkMcpPort(
        endpoint="http://test/mcp",
        timeout_seconds=1,
        call_tool=call_tool,
        sensitive_policy=SensitiveFieldPolicy(mask_fields=frozenset({"email"}), confirmed=True),
    )
    response = await port.explore(
        DataLinkExploreCommand(
            datasource_id="source",
            schema_revision=1,
            graph_version="version",
            query="orders",
        ),
        RunCancellation(),
    )
    assert not isinstance(response, AgentFailure)
    nodes = {node.name: node for node in response.result.nodes}
    assert nodes["email"].profile is not None
    assert nodes["email"].profile.typical_values == []
    assert nodes["status"].profile is not None
    assert nodes["status"].profile.typical_values == ["paid", "pending"]

    # 未绑定策略的端口保持原样（预览服务负责自行剥离）。
    plain_port = DataLinkMcpPort(endpoint="http://test/mcp", timeout_seconds=1, call_tool=call_tool)
    plain_response = await plain_port.explore(
        DataLinkExploreCommand(
            datasource_id="source",
            schema_revision=1,
            graph_version="version",
            query="orders",
        ),
        RunCancellation(),
    )
    assert not isinstance(plain_response, AgentFailure)
    plain_nodes = {node.name: node for node in plain_response.result.nodes}
    assert plain_nodes["email"].profile is not None
    assert plain_nodes["email"].profile.typical_values == ["a@example.test", "b@example.test"]


@pytest.mark.asyncio
async def test_preview_hides_masked_typical_values_in_semantic_projection():
    reader = SourceReader()
    reader.source = reader.source.model_copy(
        update={"mask_fields": ["email"], "mask_fields_confirmed": True}
    )
    raw = DataLinkExploreResult(
        datasource_id="source",
        graph_version="version",
        query="orders",
        nodes=[
            _profile_node("a", "orders", "email", ["a@example.test"]),
            _profile_node("b", "orders", "status", ["paid", "pending"]),
        ],
        retrieval_mode="keyword",
        is_truncated=False,
    )

    async def call_tool(endpoint, arguments):
        return raw.model_dump(mode="json")

    service = DataLinkPreviewService(
        reader,
        DataLinkMcpPort(endpoint="http://test/mcp", timeout_seconds=1, call_tool=call_tool),
    )
    payload = DataLinkPreviewRequest(graph_version="version", query="orders", max_nodes=5)
    result = await service.preview("source", payload, RunCancellation())

    fields = {field.column: field for field in result.semantic_context.fields}
    assert fields["email"].typical_values == []
    assert fields["status"].typical_values == ["paid", "pending"]
    assert "a@example.test" not in result.model_dump_json()
