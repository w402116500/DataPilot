"""主后端访问独立 DataLink REST 管理接口的受限客户端。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, TypeVar
from urllib.parse import quote

import httpx
from contracts.datalink import (
    DataLinkCatalogDetailRead,
    DataLinkCatalogRead,
    DataLinkDraftExploreRead,
    DataLinkDraftPreviewRequest,
    DataLinkDraftRead,
    DataLinkDraftSaveRequest,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkGraphEntriesRead,
    DataLinkGraphEntryType,
    DataLinkGraphRead,
    DataLinkHealthRead,
    DataLinkPublishRead,
    DataLinkPublishRequest,
    DataLinkRebuildConflictsRead,
    DataLinkRebuildRequest,
    DataLinkRebuildResult,
    DataLinkRelationDetailRead,
    DataLinkRelationsRead,
    DataLinkRemoveResult,
    DataLinkResolveCandidateRequest,
    DataLinkRestoreRequest,
    DataLinkStatusRead,
    DataLinkSubgraphRead,
    DataLinkVersionDiffRead,
    DataLinkVersionPublishRead,
    DataLinkVersionsRead,
)
from pydantic import BaseModel, ValidationError

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class DataLinkClientError(Exception):
    """DataLink 管理调用的稳定失败，不携带上游响应原文或内部地址。"""

    def __init__(
        self,
        code: DataLinkErrorCode,
        message: str,
        *,
        status_code: int,
        details: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


class DataLinkManagementPort(Protocol):
    """DataSource 用例需要的最小 DataLink 管理能力，方便隔离 HTTP 与测试替身。"""

    async def health(self) -> DataLinkHealthRead: ...

    async def rebuild(self, payload: DataLinkRebuildRequest) -> DataLinkRebuildResult: ...

    async def status(self, datasource_id: str) -> DataLinkStatusRead: ...

    async def graph(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
    ) -> DataLinkGraphRead: ...

    async def graph_entries(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
        entry_type: DataLinkGraphEntryType | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkGraphEntriesRead: ...

    async def graph_subgraph(
        self,
        datasource_id: str,
        *,
        root_node_id: str,
        graph_version: str | None = None,
        edge_types: list[DataLinkEdgeType] | None = None,
        hops: int = 1,
    ) -> DataLinkSubgraphRead: ...

    async def catalog(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
        node_type: str | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkCatalogRead: ...

    async def catalog_detail(
        self,
        datasource_id: str,
        node_id: str,
        *,
        graph_version: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkCatalogDetailRead: ...

    async def relations(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
        node_id: str | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkRelationsRead: ...

    async def relation(
        self,
        datasource_id: str,
        relation_id: str,
        *,
        graph_version: str,
    ) -> DataLinkRelationDetailRead: ...

    async def remove(self, datasource_id: str) -> DataLinkRemoveResult: ...

    async def draft(self, datasource_id: str) -> DataLinkDraftRead | None: ...

    async def draft_preview(
        self, datasource_id: str, payload: DataLinkDraftPreviewRequest
    ) -> DataLinkDraftExploreRead: ...

    async def save_draft(
        self, datasource_id: str, payload: DataLinkDraftSaveRequest
    ) -> DataLinkDraftRead: ...

    async def publish(
        self, datasource_id: str, payload: DataLinkPublishRequest
    ) -> DataLinkPublishRead: ...

    async def versions(
        self, datasource_id: str, *, page: int, page_size: int
    ) -> DataLinkVersionsRead: ...

    async def restore(
        self, datasource_id: str, payload: DataLinkRestoreRequest
    ) -> DataLinkVersionPublishRead: ...

    async def resolve_candidate(
        self, datasource_id: str, payload: DataLinkResolveCandidateRequest
    ) -> DataLinkVersionPublishRead: ...

    async def rebuild_conflicts(
        self, datasource_id: str, version: str
    ) -> DataLinkRebuildConflictsRead: ...

    async def version_catalog(
        self, datasource_id: str, version: str, *, node_type: str | None, page: int, page_size: int
    ) -> DataLinkCatalogRead: ...

    async def version_diff(self, datasource_id: str, version: str) -> DataLinkVersionDiffRead: ...


class DataLinkClient:
    """只调用固定 REST 地址，并只对连接失败或超时重试一次。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def health(self) -> DataLinkHealthRead:
        """读取安全健康摘要，供主后端健康检查投影 DataLink 状态。"""

        return await self._request("GET", "/health", response_model=DataLinkHealthRead)

    async def rebuild(self, payload: DataLinkRebuildRequest) -> DataLinkRebuildResult:
        """按单次请求的 rebuild key 同步构建或复用该次图谱。"""

        body = payload.model_dump(mode="json")
        if payload.connection_grant is not None:
            body["connection_grant"] = payload.connection_grant.get_secret_value()
        return await self._request(
            "POST",
            "/v1/graphs/rebuild",
            response_model=DataLinkRebuildResult,
            json=body,
        )

    async def status(self, datasource_id: str) -> DataLinkStatusRead:
        """读取最终或运行中的 Build，供超时和服务重启后的状态对账。"""

        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/status",
            response_model=DataLinkStatusRead,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        return result

    async def graph(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
    ) -> DataLinkGraphRead:
        """读取受限图谱面板数据，不请求路径、向量或原始样例。"""

        params = {"graph_version": graph_version} if graph_version is not None else None
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}",
            response_model=DataLinkGraphRead,
            params=params,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        return result

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
        """读取表和实体入口列表，仍由主后端隔离 DataLink 服务地址。"""

        params: dict[str, str] = {
            "page": str(page),
            "page_size": str(page_size),
        }
        if graph_version is not None:
            params["graph_version"] = graph_version
        if entry_type is not None:
            params["entry_type"] = entry_type
        if query is not None:
            params["query"] = query
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/entries",
            response_model=DataLinkGraphEntriesRead,
            params=params,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        return result

    async def graph_subgraph(
        self,
        datasource_id: str,
        *,
        root_node_id: str,
        graph_version: str | None = None,
        edge_types: list[DataLinkEdgeType] | None = None,
        hops: int = 1,
    ) -> DataLinkSubgraphRead:
        """读取当前根节点的一到两跳局部图，关系类型通过重复查询参数传递。"""

        params: dict[str, str | list[str]] = {
            "root_node_id": root_node_id,
            "hops": str(hops),
        }
        if graph_version is not None:
            params["graph_version"] = graph_version
        if edge_types is not None:
            params["edge_types"] = [edge_type.value for edge_type in edge_types]
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/subgraph",
            response_model=DataLinkSubgraphRead,
            params=params,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        return result

    async def remove(self, datasource_id: str) -> DataLinkRemoveResult:
        """幂等删除 DataLink 自己的图谱记录，不接触主后端上传副本。"""

        result = await self._request(
            "POST",
            f"/v1/graphs/{datasource_id}/remove",
            response_model=DataLinkRemoveResult,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        return result

    async def versions(
        self, datasource_id: str, *, page: int, page_size: int
    ) -> DataLinkVersionsRead:
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/versions",
            response_model=DataLinkVersionsRead,
            params={"page": str(page), "page_size": str(page_size)},
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if result.page != page or result.page_size != page_size:
            raise self._invalid_version_response()
        return result

    async def rebuild_conflicts(
        self, datasource_id: str, version: str
    ) -> DataLinkRebuildConflictsRead:
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/versions/{quote(version, safe='')}/conflicts",
            response_model=DataLinkRebuildConflictsRead,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if result.candidate_graph_version != version:
            raise self._invalid_version_response()
        return result

    async def version_catalog(
        self, datasource_id: str, version: str, *, node_type: str | None, page: int, page_size: int
    ) -> DataLinkCatalogRead:
        params = {"page": str(page), "page_size": str(page_size)}
        if node_type is not None:
            params["node_type"] = node_type
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/versions/{quote(version, safe='')}/catalog",
            response_model=DataLinkCatalogRead,
            params=params,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if result.graph_version != version or result.page != page or result.page_size != page_size:
            raise self._invalid_version_response()
        return result

    async def version_diff(self, datasource_id: str, version: str) -> DataLinkVersionDiffRead:
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/versions/{quote(version, safe='')}/diff",
            response_model=DataLinkVersionDiffRead,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if result.graph_version != version:
            raise self._invalid_version_response()
        return result

    async def restore(
        self, datasource_id: str, payload: DataLinkRestoreRequest
    ) -> DataLinkVersionPublishRead:
        return await self._publish_version(
            datasource_id, "restore", payload, payload.target_graph_version, "restore"
        )

    async def resolve_candidate(
        self, datasource_id: str, payload: DataLinkResolveCandidateRequest
    ) -> DataLinkVersionPublishRead:
        return await self._publish_version(
            datasource_id, "resolve-candidate", payload, payload.candidate_graph_version, "manual"
        )

    async def _publish_version(
        self,
        datasource_id: str,
        action: str,
        payload: DataLinkRestoreRequest | DataLinkResolveCandidateRequest,
        source_version: str,
        origin: str,
    ) -> DataLinkVersionPublishRead:
        result = await self._request(
            "POST",
            f"/v1/graphs/{datasource_id}/{action}",
            response_model=DataLinkVersionPublishRead,
            json=payload.model_dump(mode="json"),
            retry=False,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if (
            result.previous_graph_version != payload.expected_head
            or result.source_graph_version != source_version
            or result.idempotency_key != payload.idempotency_key
            or result.origin_kind != origin
            or not result.graph_version
            or result.graph_version in {source_version, payload.expected_head}
        ):
            raise self._invalid_version_response()
        return result

    @staticmethod
    def _invalid_version_response() -> DataLinkClientError:
        return DataLinkClientError(
            DataLinkErrorCode.INVALID_QUERY, "DataLink 返回的版本身份不一致", status_code=502
        )

    async def draft(self, datasource_id: str) -> DataLinkDraftRead | None:
        response = await self._raw_request("GET", f"/v1/graphs/{datasource_id}/draft")
        if response.is_error:
            raise self._response_error(response)
        try:
            raw = response.json()
            result = DataLinkDraftRead.model_validate(raw) if raw is not None else None
        except (ValueError, TypeError):
            raise DataLinkClientError(
                DataLinkErrorCode.INVALID_QUERY, "DataLink 草稿响应无效", status_code=502
            ) from None
        if result is not None:
            self._validate_datasource_id(datasource_id, result.datasource_id)
        return result

    async def draft_preview(
        self, datasource_id: str, payload: DataLinkDraftPreviewRequest
    ) -> DataLinkDraftExploreRead:
        result = await self._request(
            "POST",
            f"/v1/graphs/{datasource_id}/draft/preview",
            response_model=DataLinkDraftExploreRead,
            json=payload.model_dump(mode="json"),
            retry=False,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        self._validate_datasource_id(datasource_id, result.result.datasource_id)
        if (
            result.draft_revision != payload.expected_draft_revision
            or result.result.graph_version != result.base_graph_version
            or result.result.query != payload.query
            or result.result.retrieval_mode != "keyword"
        ):
            raise DataLinkClientError(
                DataLinkErrorCode.INVALID_QUERY, "DataLink 草稿检索版本不一致", status_code=502
            )
        return result

    async def save_draft(
        self, datasource_id: str, payload: DataLinkDraftSaveRequest
    ) -> DataLinkDraftRead:
        result = await self._request(
            "PUT",
            f"/v1/graphs/{datasource_id}/draft",
            response_model=DataLinkDraftRead,
            json=payload.model_dump(mode="json"),
            retry=False,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if (
            result.base_graph_version != payload.base_graph_version
            or result.schema_revision != payload.schema_revision
        ):
            raise DataLinkClientError(
                DataLinkErrorCode.INVALID_QUERY, "DataLink 草稿版本不一致", status_code=502
            )
        return result

    async def publish(
        self, datasource_id: str, payload: DataLinkPublishRequest
    ) -> DataLinkPublishRead:
        result = await self._request(
            "POST",
            f"/v1/graphs/{datasource_id}/publish",
            response_model=DataLinkPublishRead,
            json=payload.model_dump(mode="json"),
            retry=False,
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if (
            result.previous_graph_version != payload.expected_head
            or result.draft_revision != payload.expected_draft_revision
            or result.idempotency_key != payload.idempotency_key
        ):
            raise DataLinkClientError(
                DataLinkErrorCode.INVALID_QUERY, "DataLink 发布结果不一致", status_code=502
            )
        return result

    async def catalog(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
        node_type: str | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkCatalogRead:
        params = {"page": str(page), "page_size": str(page_size)}
        if graph_version:
            params["graph_version"] = graph_version
        if node_type:
            params["type"] = node_type
        if query:
            params["query"] = query
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/catalog",
            response_model=DataLinkCatalogRead,
            params=params,
        )
        self._validate_catalog_identity(datasource_id, graph_version, result)
        return result

    async def catalog_detail(
        self,
        datasource_id: str,
        node_id: str,
        *,
        graph_version: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkCatalogDetailRead:
        params = {"graph_version": graph_version, "page": str(page), "page_size": str(page_size)}
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/catalog/{quote(node_id, safe='')}",
            response_model=DataLinkCatalogDetailRead,
            params=params,
        )
        self._validate_catalog_identity(datasource_id, graph_version, result)
        if result.item.node.id != node_id:
            raise DataLinkClientError(
                DataLinkErrorCode.INVALID_QUERY, "DataLink node mismatch", status_code=502
            )
        return result

    async def relations(
        self,
        datasource_id: str,
        *,
        graph_version: str | None = None,
        node_id: str | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> DataLinkRelationsRead:
        params = {"graph_version": graph_version, "page": str(page), "page_size": str(page_size)}
        if node_id:
            params["node_id"] = node_id
        if query:
            params["query"] = query
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/relations",
            response_model=DataLinkRelationsRead,
            params=params,
        )
        self._validate_catalog_identity(datasource_id, graph_version, result)
        return result

    async def relation(
        self,
        datasource_id: str,
        relation_id: str,
        *,
        graph_version: str,
    ) -> DataLinkRelationDetailRead:
        result = await self._request(
            "GET",
            f"/v1/graphs/{datasource_id}/relations/{quote(relation_id, safe='')}",
            response_model=DataLinkRelationDetailRead,
            params={"graph_version": graph_version},
        )
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if result.graph_version != graph_version or result.item.id != relation_id:
            raise DataLinkClientError(
                DataLinkErrorCode.INVALID_QUERY, "DataLink relation mismatch", status_code=502
            )
        return result

    def _validate_catalog_identity(
        self,
        datasource_id: str,
        graph_version: str | None,
        result: DataLinkCatalogRead | DataLinkCatalogDetailRead | DataLinkRelationsRead,
    ) -> None:
        self._validate_datasource_id(datasource_id, result.datasource_id)
        if graph_version is not None and result.graph_version != graph_version:
            raise DataLinkClientError(
                DataLinkErrorCode.GRAPH_VERSION_NOT_FOUND,
                "DataLink graph version mismatch",
                status_code=502,
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        response_model: type[ResponseModel],
        json: dict[str, object] | None = None,
        params: dict[str, str | list[str] | None] | None = None,
        retry: bool = True,
    ) -> ResponseModel:
        """执行一个受限请求；网络重试不会把参数或响应内容写入异常。"""

        response: httpx.Response | None = None
        attempts = 2 if retry else 1
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = await client.request(method, path, json=json, params=params)
                break
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt == attempts - 1:
                    raise DataLinkClientError(
                        DataLinkErrorCode.DATALINK_UNAVAILABLE,
                        "DataLink 服务暂时不可用",
                        status_code=503,
                    ) from None

        if response is None:
            raise AssertionError("DataLink request did not produce a response")
        if response.is_error:
            raise self._response_error(response)
        try:
            return response_model.model_validate(response.json())
        except (TypeError, ValueError, ValidationError):
            raise DataLinkClientError(
                DataLinkErrorCode.BUILD_FAILED,
                "DataLink 返回了不符合约定的结果",
                status_code=502,
            ) from None

    async def _raw_request(self, method: str, path: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout_seconds, transport=self.transport
            ) as client:
                return await client.request(method, path)
        except (httpx.ConnectError, httpx.TimeoutException):
            raise DataLinkClientError(
                DataLinkErrorCode.DATALINK_UNAVAILABLE, "DataLink 服务暂时不可用", status_code=503
            ) from None

    @staticmethod
    def _response_error(response: httpx.Response) -> DataLinkClientError:
        """只采信约定错误码和 build_id，避免把上游报错内容回显到主后端。"""

        payload: object
        try:
            payload = response.json()
        except ValueError:
            payload = None

        error = payload.get("error") if isinstance(payload, Mapping) else None
        code_value = error.get("code") if isinstance(error, Mapping) else None
        try:
            code = DataLinkErrorCode(str(code_value))
        except ValueError:
            code = DataLinkErrorCode.BUILD_FAILED

        details: dict[str, str] = {}
        raw_details = error.get("details") if isinstance(error, Mapping) else None
        build_id = raw_details.get("build_id") if isinstance(raw_details, Mapping) else None
        if isinstance(build_id, str) and build_id:
            details["build_id"] = build_id
        return DataLinkClientError(
            code,
            _message_for(code),
            status_code=response.status_code,
            details=details,
        )

    @staticmethod
    def _validate_datasource_id(expected: str, actual: str) -> None:
        """拒绝上游把其他 DataSource 的结果投影到当前请求，防止跨源混用。"""

        if actual != expected:
            raise DataLinkClientError(
                DataLinkErrorCode.DATASOURCE_MISMATCH,
                "DataLink 返回的结果不属于当前数据源",
                status_code=502,
            )


def _message_for(code: DataLinkErrorCode) -> str:
    """将 DataLink 错误收敛成主后端可安全展示的简短中文说明。"""

    messages = {
        DataLinkErrorCode.DRAFT_STALE: "草稿已被修改，请刷新后重试",
        DataLinkErrorCode.HEAD_STALE: "发布基线已变化，请刷新后处理冲突",
        DataLinkErrorCode.REVISION_INVALID: "修订对象或关系端点无效",
        DataLinkErrorCode.IDEMPOTENCY_CONFLICT: "发布标识已用于另一次请求",
        DataLinkErrorCode.DATALINK_UNAVAILABLE: "DataLink 服务暂时不可用",
        DataLinkErrorCode.GRAPH_VERSION_NOT_FOUND: "请求的 DataLink 图谱版本不存在",
        DataLinkErrorCode.DATASOURCE_MISMATCH: "DataLink 图谱不属于当前数据源",
        DataLinkErrorCode.INVALID_QUERY: "DataLink 请求参数无效",
        DataLinkErrorCode.PATH_OUTSIDE_ROOT: "DataLink 数据源文件引用不安全",
        DataLinkErrorCode.BUILD_ALREADY_RUNNING: "DataLink 图谱正在构建中",
        DataLinkErrorCode.BUILD_FAILED: "DataLink 构建失败",
        DataLinkErrorCode.BUILD_INTERRUPTED: "DataLink 构建被服务重启打断",
        DataLinkErrorCode.MODEL_CONFIG_INVALID: "DataLink 模型配置无效",
        DataLinkErrorCode.MODEL_REQUEST_TIMEOUT: "DataLink 语义模型请求超时",
        DataLinkErrorCode.MODEL_UPSTREAM_ERROR: "DataLink 语义模型服务异常",
        DataLinkErrorCode.MODEL_RESPONSE_ERROR: "DataLink 语义模型未返回可用结果",
        DataLinkErrorCode.MODEL_RESPONSE_INVALID: "DataLink 语义模型返回内容不符合约定",
        DataLinkErrorCode.SEMANTIC_MAPPING_INVALID: "DataLink 语义映射结果不符合约定",
    }
    return messages[code]
