"""主后端与独立 DataLink 的生命周期、错误收尾和 HTTP 边界测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from application.datalink_client import DataLinkClient, DataLinkClientError
from contracts.datalink import (
    DataLinkBuildRead,
    DataLinkBuildStatus,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkGraphEntriesRead,
    DataLinkGraphEntryRead,
    DataLinkGraphEntryType,
    DataLinkGraphRead,
    DataLinkHealthRead,
    DataLinkRebuildRequest,
    DataLinkRebuildResult,
    DataLinkRemoveResult,
    DataLinkStatusRead,
    DataLinkSubgraphRead,
)
from fastapi.testclient import TestClient
from server.app import create_app


class StatefulDataLinkClient:
    """可控制的 DataLink 管理端口，用于主后端状态机集成测试。"""

    def __init__(self) -> None:
        self.builds: dict[str, DataLinkBuildRead] = {}
        self.rebuild_calls: list[DataLinkRebuildRequest] = []
        self.rebuild_error: DataLinkClientError | None = None
        self.remove_error: DataLinkClientError | None = None
        self.remove_calls = 0
        self.entries_calls: list[dict[str, object]] = []
        self.subgraph_calls: list[dict[str, object]] = []

    async def health(self) -> DataLinkHealthRead:
        return DataLinkHealthRead(
            status="healthy",
            graph_database="ok",
            source_root="ok",
            model_configured=True,
            mcp_mounted=True,
        )

    async def rebuild(self, payload: DataLinkRebuildRequest) -> DataLinkRebuildResult:
        self.rebuild_calls.append(payload)
        if self.rebuild_error is not None:
            raise self.rebuild_error
        build = self.complete_build(payload.datasource_id, payload.schema_revision)
        return DataLinkRebuildResult(
            build_id=build.build_id,
            datasource_id=build.datasource_id,
            status=build.status,
            requested_schema_revision=build.schema_revision,
            graph_version=build.graph_version,
        )

    async def status(self, datasource_id: str) -> DataLinkStatusRead:
        build = self.builds.get(datasource_id)
        return DataLinkStatusRead(
            datasource_id=datasource_id,
            current_graph_version=build.graph_version if build else None,
            current_build=build,
        )

    async def graph(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
    ) -> DataLinkGraphRead:
        build = self.builds.get(datasource_id)
        return DataLinkGraphRead(
            datasource_id=datasource_id,
            graph_version=graph_version or (build.graph_version if build else None),
        )

    async def graph_entries(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
        entry_type: DataLinkGraphEntryType | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkGraphEntriesRead:
        self.entries_calls.append(
            {
                "datasource_id": datasource_id,
                "graph_version": graph_version,
                "entry_type": entry_type,
                "query": query,
                "page": page,
                "page_size": page_size,
            }
        )
        build = self.builds.get(datasource_id)
        return DataLinkGraphEntriesRead(
            datasource_id=datasource_id,
            graph_version=graph_version or (build.graph_version if build else "graph_missing"),
            items=[
                DataLinkGraphEntryRead(
                    id=f"table:{datasource_id}:orders",
                    type="table",
                    name="orders",
                )
            ],
            page=page,
            page_size=page_size,
            total=1,
        )

    async def graph_subgraph(
        self,
        datasource_id: str,
        *,
        root_node_id: str,
        graph_version: str | None = None,
        edge_types: list[DataLinkEdgeType] | None = None,
        hops: int = 1,
    ) -> DataLinkSubgraphRead:
        self.subgraph_calls.append(
            {
                "datasource_id": datasource_id,
                "root_node_id": root_node_id,
                "graph_version": graph_version,
                "edge_types": edge_types,
                "hops": hops,
            }
        )
        build = self.builds.get(datasource_id)
        return DataLinkSubgraphRead(
            datasource_id=datasource_id,
            graph_version=graph_version or (build.graph_version if build else "graph_missing"),
            root_node_id=root_node_id,
            total_node_count=1,
            total_edge_count=0,
            is_truncated=False,
        )

    async def remove(self, datasource_id: str) -> DataLinkRemoveResult:
        self.remove_calls += 1
        if self.remove_error is not None:
            raise self.remove_error
        return DataLinkRemoveResult(
            datasource_id=datasource_id,
            removed=self.builds.pop(datasource_id, None) is not None,
        )

    def complete_build(self, datasource_id: str, schema_revision: int) -> DataLinkBuildRead:
        """模拟 DataLink 已完成但主后端尚未拿到 rebuild 响应的状态。"""

        previous = self.builds.get(datasource_id)
        attempt_no = (
            previous.attempt_no + 1
            if previous is not None and previous.schema_revision == schema_revision
            else 1
        )
        suffix = "" if attempt_no == 1 else f"_{attempt_no}"
        build = DataLinkBuildRead(
            build_id=f"build_{datasource_id}_{schema_revision}{suffix}",
            datasource_id=datasource_id,
            schema_revision=schema_revision,
            attempt_no=attempt_no,
            status=DataLinkBuildStatus.COMPLETED,
            graph_version=f"graph_{datasource_id}_{schema_revision}{suffix}",
            finished_at=datetime.now(UTC),
        )
        self.builds[datasource_id] = build
        return build


def unwrap(response):
    body = response.json()
    assert body["error"] is None
    return body["data"]


def upload_csv(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/datasources/upload",
        data={"type": "csv", "name": "DataLink orders"},
        files={"file": ("orders.csv", b"order_id,amount\n1,10.20\n", "text/csv")},
    )
    assert response.status_code == 202
    return unwrap(response)


def test_upload_stays_schema_ready_until_explicit_rebuild_then_shares_graph_version(
    migrated_settings,
) -> None:
    datalink = StatefulDataLinkClient()
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        uploaded = upload_csv(client)
        datasource_id = uploaded["id"]

        detail = unwrap(client.get(f"/datasources/{datasource_id}"))
        status = unwrap(client.get(f"/datasources/{datasource_id}/datalink/status"))
        graph = client.get(f"/datasources/{datasource_id}/datalink/graph")

        assert detail["status"] == "schema_ready"
        assert detail["datalink_build_id"] is None
        assert detail["datalink_graph_version"] is None
        assert status["current_graph_version"] is None
        assert status["current_build"] is None
        assert graph.status_code == 409
        assert graph.json()["error"]["code"] == "DATASOURCE_NOT_READY"
        assert datalink.rebuild_calls == []

        rebuild_response = client.post(f"/datasources/{datasource_id}/datalink/rebuild")
        assert rebuild_response.status_code == 202
        rebuilt = unwrap(rebuild_response)
        detail = unwrap(client.get(f"/datasources/{datasource_id}"))
        status = unwrap(client.get(f"/datasources/{datasource_id}/datalink/status"))
        graph = unwrap(client.get(f"/datasources/{datasource_id}/datalink/graph"))

    assert detail["status"] == "ready"
    assert detail["datalink_build_id"] == rebuilt["build_id"]
    assert detail["datalink_graph_version"] == rebuilt["graph_version"]
    assert status["current_graph_version"] == detail["datalink_graph_version"]
    assert graph["graph_version"] == detail["datalink_graph_version"]
    assert rebuilt["status"] == "completed"
    assert len(datalink.rebuild_calls) == 1
    assert datalink.rebuild_calls[0].datasource_id == datasource_id


def test_rebuild_response_timeout_stays_building_until_status_confirms_completion(
    migrated_settings,
) -> None:
    datalink = StatefulDataLinkClient()
    datalink.rebuild_error = DataLinkClientError(
        DataLinkErrorCode.DATALINK_UNAVAILABLE,
        "DataLink 服务暂时不可用",
        status_code=503,
    )
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        uploaded = upload_csv(client)
        datasource_id = str(uploaded["id"])

        failed_rebuild = client.post(f"/datasources/{datasource_id}/datalink/rebuild")
        building = unwrap(client.get(f"/datasources/{datasource_id}"))
        datalink.rebuild_error = None
        datalink.complete_build(datasource_id, 1)
        status = unwrap(client.get(f"/datasources/{datasource_id}/datalink/status"))
        reconciled = unwrap(client.get(f"/datasources/{datasource_id}"))

    assert failed_rebuild.status_code == 503
    assert failed_rebuild.json()["error"]["code"] == "DATALINK_UNAVAILABLE"
    assert building["status"] == "building_datalink"
    assert building["last_error_code"] == "DATALINK_UNAVAILABLE"
    assert status["current_build"]["status"] == "completed"
    assert reconciled["status"] == "ready"
    assert reconciled["datalink_graph_version"] == status["current_graph_version"]


def test_duplicate_rebuild_keeps_active_build_and_reconciles_when_it_completes(
    migrated_settings,
) -> None:
    datalink = StatefulDataLinkClient()
    datalink.rebuild_error = DataLinkClientError(
        DataLinkErrorCode.BUILD_ALREADY_RUNNING,
        "DataLink 图谱正在构建中",
        status_code=409,
        details={"build_id": "build_active_1"},
    )
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        datasource_id = str(upload_csv(client)["id"])

        conflict = client.post(f"/datasources/{datasource_id}/datalink/rebuild")
        building = unwrap(client.get(f"/datasources/{datasource_id}"))
        datalink.rebuild_error = None
        datalink.complete_build(datasource_id, 1)
        status = unwrap(client.get(f"/datasources/{datasource_id}/datalink/status"))
        reconciled = unwrap(client.get(f"/datasources/{datasource_id}"))

    assert conflict.status_code == 409
    assert conflict.json()["error"] == {
        "code": DataLinkErrorCode.BUILD_ALREADY_RUNNING.value,
        "message": "DataLink 图谱正在构建中",
        "details": {"build_id": "build_active_1"},
    }
    assert building["status"] == "building_datalink"
    assert building["last_error_code"] is None
    assert status["current_build"]["status"] == "completed"
    assert reconciled["status"] == "ready"
    assert reconciled["datalink_graph_version"] == status["current_graph_version"]


def test_status_restores_completed_build_after_a_prior_failed_projection(migrated_settings) -> None:
    datalink = StatefulDataLinkClient()
    datalink.rebuild_error = DataLinkClientError(
        DataLinkErrorCode.BUILD_FAILED,
        "DataLink 构建失败",
        status_code=409,
    )
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        datasource_id = str(upload_csv(client)["id"])

        failed_rebuild = client.post(f"/datasources/{datasource_id}/datalink/rebuild")
        failed = unwrap(client.get(f"/datasources/{datasource_id}"))
        datalink.rebuild_error = None
        datalink.complete_build(datasource_id, 1)
        status = unwrap(client.get(f"/datasources/{datasource_id}/datalink/status"))
        recovered = unwrap(client.get(f"/datasources/{datasource_id}"))

    assert failed_rebuild.status_code == 409
    assert failed["status"] == "schema_ready"
    assert failed["last_error_code"] == DataLinkErrorCode.BUILD_FAILED.value
    assert status["current_build"]["status"] == "completed"
    assert recovered["status"] == "ready"
    assert recovered["datalink_graph_version"] == status["current_graph_version"]


def test_graph_browser_endpoints_proxy_confirmed_version_and_preserve_query_contract(
    migrated_settings,
) -> None:
    datalink = StatefulDataLinkClient()
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        datasource_id = str(upload_csv(client)["id"])
        rebuilt = unwrap(client.post(f"/datasources/{datasource_id}/datalink/rebuild"))
        entries = unwrap(
            client.get(
                f"/datasources/{datasource_id}/datalink/entries",
                params={
                    "entry_type": "table",
                    "query": "order",
                    "page": 2,
                    "page_size": 10,
                },
            )
        )
        subgraph = unwrap(
            client.get(
                f"/datasources/{datasource_id}/datalink/subgraph",
                params=[
                    ("root_node_id", f"table:{datasource_id}:orders"),
                    ("edge_types", DataLinkEdgeType.CONTAINS.value),
                    ("edge_types", DataLinkEdgeType.FOREIGN_KEY.value),
                    ("hops", "2"),
                ],
            )
        )

    assert entries["graph_version"] == rebuilt["graph_version"]
    assert subgraph["graph_version"] == rebuilt["graph_version"]
    assert datalink.entries_calls == [
        {
            "datasource_id": datasource_id,
            "graph_version": rebuilt["graph_version"],
            "entry_type": "table",
            "query": "order",
            "page": 2,
            "page_size": 10,
        }
    ]
    assert datalink.subgraph_calls == [
        {
            "datasource_id": datasource_id,
            "root_node_id": f"table:{datasource_id}:orders",
            "graph_version": rebuilt["graph_version"],
            "edge_types": [DataLinkEdgeType.CONTAINS, DataLinkEdgeType.FOREIGN_KEY],
            "hops": 2,
        }
    ]


def test_api_restart_reconciles_a_completed_initial_build(migrated_settings) -> None:
    datalink = StatefulDataLinkClient()
    datalink.rebuild_error = DataLinkClientError(
        DataLinkErrorCode.DATALINK_UNAVAILABLE,
        "DataLink 服务暂时不可用",
        status_code=503,
    )
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        datasource_id = str(upload_csv(client)["id"])
        failed_rebuild = client.post(f"/datasources/{datasource_id}/datalink/rebuild")
        assert failed_rebuild.status_code == 503
        assert unwrap(client.get(f"/datasources/{datasource_id}"))["status"] == "building_datalink"

    datalink.rebuild_error = None
    datalink.complete_build(datasource_id, 1)
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        reconciled = unwrap(client.get(f"/datasources/{datasource_id}"))

    assert reconciled["status"] == "ready"
    assert reconciled["datalink_graph_version"] == f"graph_{datasource_id}_1"
    assert len(datalink.rebuild_calls) == 1


def test_rebuilding_an_existing_graph_keeps_ready_when_datalink_is_unavailable(
    migrated_settings,
) -> None:
    datalink = StatefulDataLinkClient()
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        datasource_id = str(upload_csv(client)["id"])
        initial_rebuild = unwrap(client.post(f"/datasources/{datasource_id}/datalink/rebuild"))
        original = unwrap(client.get(f"/datasources/{datasource_id}"))
        datalink.rebuild_error = DataLinkClientError(
            DataLinkErrorCode.DATALINK_UNAVAILABLE,
            "DataLink 服务暂时不可用",
            status_code=503,
        )

        failed_rebuild = client.post(f"/datasources/{datasource_id}/datalink/rebuild")
        preserved = unwrap(client.get(f"/datasources/{datasource_id}"))

    assert failed_rebuild.status_code == 503
    assert initial_rebuild["status"] == "completed"
    assert preserved["status"] == "ready"
    assert preserved["datalink_graph_version"] == original["datalink_graph_version"]
    assert preserved["last_error_code"] == "DATALINK_UNAVAILABLE"


def test_rebuilding_an_existing_graph_creates_a_new_version_after_success(
    migrated_settings,
) -> None:
    datalink = StatefulDataLinkClient()
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        datasource_id = str(upload_csv(client)["id"])
        initial = unwrap(client.post(f"/datasources/{datasource_id}/datalink/rebuild"))
        rebuilt = unwrap(client.post(f"/datasources/{datasource_id}/datalink/rebuild"))
        detail = unwrap(client.get(f"/datasources/{datasource_id}"))
        status = unwrap(client.get(f"/datasources/{datasource_id}/datalink/status"))

    assert rebuilt["build_id"] != initial["build_id"]
    assert rebuilt["graph_version"] != initial["graph_version"]
    assert detail["status"] == "ready"
    assert detail["datalink_build_id"] == rebuilt["build_id"]
    assert detail["datalink_graph_version"] == rebuilt["graph_version"]
    assert status["current_graph_version"] == rebuilt["graph_version"]
    assert len(datalink.rebuild_calls) == 2


def test_graph_read_does_not_fallback_to_an_unconfirmed_datalink_head_after_schema_change(
    migrated_settings,
) -> None:
    datalink = StatefulDataLinkClient()
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        datasource_id = str(upload_csv(client)["id"])
        initial_rebuild = unwrap(client.post(f"/datasources/{datasource_id}/datalink/rebuild"))
        source_path = migrated_settings.datasource_root / datasource_id / "source.csv"
        source_path.write_text(
            "order_id,amount,channel\n1,10.20,web\n",
            encoding="utf-8",
        )
        datalink.rebuild_error = DataLinkClientError(
            DataLinkErrorCode.DATALINK_UNAVAILABLE,
            "DataLink 服务暂时不可用",
            status_code=503,
        )

        retried = client.post(f"/datasources/{datasource_id}/test")
        assert retried.status_code == 202
        assert unwrap(client.get(f"/datasources/{datasource_id}"))["status"] == ("schema_ready")

        graph = client.get(f"/datasources/{datasource_id}/datalink/graph")

    assert graph.status_code == 409
    assert graph.json()["error"]["code"] == "DATASOURCE_NOT_READY"
    assert initial_rebuild["status"] == "completed"
    assert len(datalink.rebuild_calls) == 1


def test_delete_keeps_deleting_after_datalink_failure_and_retries_cleanly(
    migrated_settings,
) -> None:
    datalink = StatefulDataLinkClient()
    with TestClient(create_app(migrated_settings, datalink_client=datalink)) as client:
        datasource_id = str(upload_csv(client)["id"])
        datalink.remove_error = DataLinkClientError(
            DataLinkErrorCode.DATALINK_UNAVAILABLE,
            "DataLink 服务暂时不可用",
            status_code=503,
        )

        failed_delete = client.delete(f"/datasources/{datasource_id}")
        deleting = unwrap(client.get(f"/datasources/{datasource_id}"))
        preview = client.get(f"/datasources/{datasource_id}/tables/dataset/preview")

        datalink.remove_error = None
        deleted = unwrap(client.delete(f"/datasources/{datasource_id}"))

    assert failed_delete.status_code == 503
    assert failed_delete.json()["error"]["code"] == "DATALINK_UNAVAILABLE"
    assert deleting["status"] == "deleting"
    assert deleting["last_error_code"] == "DATALINK_UNAVAILABLE"
    assert preview.status_code == 409
    assert deleted == {"datasource_id": datasource_id, "status": "deleted"}
    assert datalink.remove_calls == 2


def test_datalink_http_client_retries_a_connection_failure_once() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(
            200,
            json={
                "build_id": "build_1",
                "datasource_id": "datasource_1",
                "status": "completed",
                "requested_schema_revision": 1,
                "graph_version": "graph_1",
            },
        )

    datalink = DataLinkClient(
        "http://datalink.test",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        datalink.rebuild(
            DataLinkRebuildRequest(
                datasource_id="datasource_1",
                rebuild_key="rebuild_1",
                source_type="csv",
                source_ref="datasource_1/source.csv",
                schema_revision=1,
            )
        )
    )

    assert attempts == 2
    assert result.graph_version == "graph_1"


@pytest.mark.parametrize(
    ("code", "expected_message"),
    [
        (DataLinkErrorCode.BUILD_FAILED, "DataLink 构建失败"),
        (DataLinkErrorCode.BUILD_ALREADY_RUNNING, "DataLink 图谱正在构建中"),
    ],
)
def test_datalink_http_client_does_not_retry_explicit_build_response(
    code: DataLinkErrorCode,
    expected_message: str,
) -> None:
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            409,
            json={
                "data": None,
                "request_id": "req_datalink",
                "error": {
                    "code": code.value,
                    "message": "Internal message must not escape",
                    "details": {"build_id": "build_1"},
                },
            },
        )

    datalink = DataLinkClient(
        "http://datalink.test",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(DataLinkClientError) as caught:
        asyncio.run(
            datalink.rebuild(
                DataLinkRebuildRequest(
                    datasource_id="datasource_1",
                    rebuild_key="rebuild_1",
                    source_type="csv",
                    source_ref="datasource_1/source.csv",
                    schema_revision=1,
                )
            )
        )

    assert attempts == 1
    assert caught.value.code == code
    assert caught.value.message == expected_message
    assert caught.value.details == {"build_id": "build_1"}


def test_datalink_http_client_encodes_repeated_subgraph_edge_types() -> None:
    received_params: dict[str, list[str]] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        received_params["edge_types"] = request.url.params.get_list("edge_types")
        received_params["graph_version"] = request.url.params.get_list("graph_version")
        return httpx.Response(
            200,
            json={
                "datasource_id": "datasource_1",
                "graph_version": "graph_1",
                "root_node_id": "table:datasource_1:orders",
                "total_node_count": 3,
                "total_edge_count": 2,
                "nodes": [],
                "edges": [],
                "is_truncated": False,
                "warnings": [],
            },
        )

    datalink = DataLinkClient(
        "http://datalink.test",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        datalink.graph_subgraph(
            "datasource_1",
            graph_version="graph_1",
            root_node_id="table:datasource_1:orders",
            edge_types=[DataLinkEdgeType.CONTAINS, DataLinkEdgeType.FOREIGN_KEY],
            hops=2,
        )
    )

    assert received_params["edge_types"] == ["contains", "foreign_key"]
    assert received_params["graph_version"] == ["graph_1"]
    assert result.root_node_id == "table:datasource_1:orders"


def test_datalink_http_client_omits_unselected_entry_filters() -> None:
    received_params: dict[str, list[str]] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        received_params["graph_version"] = request.url.params.get_list("graph_version")
        received_params["entry_type"] = request.url.params.get_list("entry_type")
        received_params["query"] = request.url.params.get_list("query")
        return httpx.Response(
            200,
            json={
                "datasource_id": "datasource_1",
                "graph_version": "graph_1",
                "items": [],
                "page": 1,
                "page_size": 50,
                "total": 0,
            },
        )

    datalink = DataLinkClient(
        "http://datalink.test",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(datalink.graph_entries("datasource_1"))

    assert received_params == {"graph_version": [], "entry_type": [], "query": []}
    assert result.items == []
