"""阶段四把既有 Data Gateway 和独立 DataLink 接入 Graph Port 的适配器。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    DataLinkExploreCommand,
    DataLinkExploreResponse,
    SchemaContext,
    SchemaLoadRequest,
    SqlExecutionFailure,
    SqlExecutionRequest,
    SqlExecutionResult,
)
from agent_runtime.ports import CancellationSignal
from contracts.datalink import DataLinkErrorCode, DataLinkExploreRequest, DataLinkExploreResult
from contracts.datasources import SchemaSummaryRead
from contracts.errors import AppError
from contracts.status import DataSourceStatus
from data_gateway.exceptions import (
    QueryCanceledError,
    QueryFailedError,
    QueryTimeoutError,
    SqlGuardBlockedError,
)
from data_gateway.types import GatewaySourceSnapshot, QueryCancelToken, SourceAccess
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from metadata.repositories import DataSourceRepository
from pydantic import ValidationError

from application.datasources import DataSourceService
from application.model_runtime import RunDeadline
from application.runtime_wait import (
    RuntimeWaitCanceled,
    RuntimeWaitTimedOut,
    await_runtime_call,
)

McpToolCaller = Callable[[str, dict[str, object]], Awaitable[object]]


class DataGatewayAgentPort:
    """复用 DataSourceService 和 Data Gateway 的 Schema、Guard、Audit、Artifact 链路。"""

    def __init__(
        self,
        *,
        datasources: DataSourceService,
        session_id: str,
        schema_revision: int,
        input_snapshot_path: str | None = None,
        deadline: RunDeadline | None = None,
        source_snapshot: GatewaySourceSnapshot | None = None,
        source_access: SourceAccess | None = None,
    ) -> None:
        self._datasources = datasources
        self._session_id = session_id
        self._schema_revision = schema_revision
        self._deadline = deadline
        self._source_snapshot = source_snapshot
        self._source_access = source_access
        self._input_snapshot_path = (
            Path(input_snapshot_path).resolve() if input_snapshot_path else None
        )

    async def load_schema(
        self,
        request: SchemaLoadRequest,
        cancellation: CancellationSignal,
    ) -> SchemaContext | AgentFailure:
        """读取已固化的 Schema，并拒绝未 ready 或 revision 不匹配的数据源。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        try:
            source = await self._datasources.get(request.datasource_id)
        except Exception:
            return _gateway_failure("数据源不可用于分析")
        if self._source_snapshot is not None:
            return _frozen_schema(self._source_snapshot, source.status, request)
        if (
            source.status not in {DataSourceStatus.SCHEMA_READY, DataSourceStatus.READY}
            or source.schema_revision != request.schema_revision
            or source.schema_summary is None
        ):
            return _gateway_failure("数据源 Schema 与当前 Run 不一致")
        if cancellation.is_cancelled():
            return _cancelled_failure()
        return SchemaContext(
            datasource_id=source.id,
            schema_revision=source.schema_revision,
            schema_summary=source.schema_summary,
        )

    async def execute_readonly(
        self,
        request: SqlExecutionRequest,
        cancellation: CancellationSignal,
    ) -> SqlExecutionResult | SqlExecutionFailure | AgentFailure:
        """把 Graph SQL 交给既有 Gateway，保留其 Guard、Audit 和表格 Artifact 所有权。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        try:
            source = await self._datasources.get(request.datasource_id)
        except Exception:
            return _gateway_failure("数据源不可用于分析")
        if self._source_snapshot is not None:
            if (
                source.status in {DataSourceStatus.DELETING, DataSourceStatus.DELETED}
                or self._source_snapshot.id != request.datasource_id
                or self._source_snapshot.schema_revision != self._schema_revision
                or self._source_access is None
            ):
                return _gateway_failure("数据源 Schema 与当前 Run 不一致")
        elif (
            source.status not in {DataSourceStatus.SCHEMA_READY, DataSourceStatus.READY}
            or source.schema_revision != self._schema_revision
        ):
            return _gateway_failure("数据源 Schema 与当前 Run 不一致")
        token = QueryCancelToken()
        timeout_seconds = self._deadline.remaining_seconds() if self._deadline is not None else None
        if timeout_seconds is not None and timeout_seconds < 1:
            return AgentFailure(
                code=AgentErrorCode.ANALYSIS_LIMIT_REACHED,
                message="SQL 查询没有剩余 Run 时间",
            )
        watcher = asyncio.create_task(_forward_cancellation(token, cancellation))
        try:
            sql_kwargs: dict[str, object] = {
                "datasource_id": request.datasource_id,
                "run_id": request.run_id,
                "session_id": self._session_id,
                "sql": request.sql,
                "cancel_token": token,
                "input_snapshot_path": self._input_snapshot_path,
                "tool_call_id": request.tool_call_id,
                "repaired_from_audit_id": request.repaired_from_audit_id,
            }
            if request.max_rows is not None:
                sql_kwargs["max_rows"] = request.max_rows
            if self._source_snapshot is not None:
                sql_kwargs["source_snapshot"] = self._source_snapshot
                sql_kwargs["source_access"] = self._source_access
            if request.artifact_usage != "query_result":
                sql_kwargs["artifact_usage"] = request.artifact_usage
            if timeout_seconds is not None:
                sql_kwargs["timeout_seconds"] = min(timeout_seconds, 600.0)
            result = await self._datasources.run_agent_sql(
                **sql_kwargs,
            )
        except QueryCanceledError:
            return _cancelled_failure()
        except SqlGuardBlockedError as exc:
            issue = exc.issue
            return SqlExecutionFailure(
                code=AgentErrorCode.DATA_GATEWAY_BLOCKED,
                reason_code=issue.reason_code,
                subject_kind=issue.subject_kind,
                subject=issue.subject,
                location=(
                    {"line": issue.location.line, "column": issue.location.column}
                    if issue.location is not None
                    else None
                ),
                message=issue.message,
                hint=issue.hint,
                audit_log_id=exc.audit_log_id,
                retryable=issue.retryable,
            )
        except QueryTimeoutError as exc:
            if not exc.audit_log_id:
                return _gateway_failure("SQL 查询失败", retryable=True)
            return SqlExecutionFailure(
                code=AgentErrorCode.DATA_GATEWAY_FAILED,
                reason_code="QUERY_TIMEOUT",
                subject_kind="statement",
                message="SQL 查询超时",
                audit_log_id=exc.audit_log_id,
                execution_status="not_started",
                retryable=True,
            )
        except QueryFailedError as exc:
            if not exc.audit_log_id:
                return _gateway_failure("SQL 查询失败")
            return SqlExecutionFailure(
                code=AgentErrorCode.DATA_GATEWAY_FAILED,
                reason_code=exc.code,
                subject_kind="statement",
                subject=None,
                message=exc.message,
                hint=exc.hint,
                audit_log_id=exc.audit_log_id,
                execution_status="not_started",
                retryable=exc.retryable,
            )
        except (AppError, ValueError):
            return _gateway_failure("数据源不可用于分析")
        except Exception:
            return _gateway_failure("SQL 查询失败")
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        if cancellation.is_cancelled():
            return _cancelled_failure()
        if result.artifact_id is None:
            return _gateway_failure("SQL 结果缺少可追溯表格产物")
        return SqlExecutionResult(
            result=result,
            audit_log_id=result.audit_log_id,
            artifact_id=result.artifact_id,
            elapsed_ms=result.elapsed_ms,
        )


class DataGatewaySchemaPort:
    """只读 Schema 端口；Opening/general 不需要装配 SQL、Artifact 或 DataLink。"""

    def __init__(
        self,
        repository: DataSourceRepository,
        source_snapshot: GatewaySourceSnapshot | None = None,
    ) -> None:
        self._repository = repository
        self._source_snapshot = source_snapshot

    async def load_schema(
        self,
        request: SchemaLoadRequest,
        cancellation: CancellationSignal,
    ) -> SchemaContext | AgentFailure:
        if cancellation.is_cancelled():
            return _cancelled_failure()
        try:
            source = await self._repository.get(request.datasource_id)
        except Exception:
            return _gateway_failure("数据源不可用于分析")
        if source is None:
            return _gateway_failure("数据源不可用于分析")
        if self._source_snapshot is not None:
            return _frozen_schema(self._source_snapshot, source.status, request)
        if (
            source.status not in {DataSourceStatus.SCHEMA_READY, DataSourceStatus.READY}
            or source.schema_revision != request.schema_revision
            or source.schema_cache_json is None
        ):
            return _gateway_failure("数据源 Schema 与当前 Run 不一致")
        try:
            summary = SchemaSummaryRead.model_validate(source.schema_cache_json)
        except (TypeError, ValueError):
            return _gateway_failure("数据源 Schema 与当前 Run 不一致")
        if cancellation.is_cancelled():
            return _cancelled_failure()
        return SchemaContext(
            datasource_id=source.id,
            schema_revision=source.schema_revision,
            schema_summary=summary,
        )


def _frozen_schema(
    snapshot: GatewaySourceSnapshot, status: str, request: SchemaLoadRequest
) -> SchemaContext | AgentFailure:
    """配置更新不改变 Run 输入，删除门禁仍即时生效。"""
    if (
        status in {DataSourceStatus.DELETING, DataSourceStatus.DELETED}
        or snapshot.id != request.datasource_id
        or snapshot.schema_revision != request.schema_revision
    ):
        return _gateway_failure("数据源 Schema 与当前 Run 不一致")
    try:
        summary = SchemaSummaryRead.model_validate(snapshot.schema_cache_json)
    except (TypeError, ValueError):
        return _gateway_failure("数据源 Schema 与当前 Run 不一致")
    return SchemaContext(
        datasource_id=snapshot.id,
        schema_revision=snapshot.schema_revision,
        schema_summary=summary,
    )


class DataLinkMcpPort:
    """只经 Streamable HTTP 调用固定 ``datalink_explore`` MCP 工具。"""

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_seconds: float,
        call_tool: McpToolCaller | None = None,
    ) -> None:
        if not endpoint or timeout_seconds <= 0:
            raise ValueError("DataLink MCP configuration is invalid")
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._call_tool = call_tool or self._call_datalink_explore

    async def explore(
        self,
        request: DataLinkExploreCommand,
        cancellation: CancellationSignal,
    ) -> DataLinkExploreResponse | AgentFailure:
        """调用唯一白名单工具，并严格验证 DataSource、图版本和问题身份。"""

        if cancellation.is_cancelled():
            return _cancelled_failure()
        arguments = DataLinkExploreRequest(
            datasource_id=request.datasource_id,
            graph_version=request.graph_version,
            query=request.query,
            focus=request.focus,
            max_nodes=request.max_nodes,
        ).model_dump(mode="json", exclude_none=True)
        try:
            raw_result = await await_runtime_call(
                self._call_tool(self._endpoint, arguments),
                cancellation=cancellation,
                timeout_seconds=self._timeout_seconds,
            )
        except RuntimeWaitCanceled:
            return _cancelled_failure()
        except RuntimeWaitTimedOut:
            return _datalink_unavailable()
        except Exception:
            return _datalink_unavailable()
        if cancellation.is_cancelled():
            return _cancelled_failure()
        try:
            result = _parse_mcp_result(raw_result)
        except _McpToolFailure as exc:
            return _map_tool_failure(exc.code)
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
            return _datalink_invalid()
        if (
            result.datasource_id != request.datasource_id
            or result.graph_version != request.graph_version
            or result.query != request.query
        ):
            return _datalink_invalid("DataLink 返回版本与当前请求不一致")
        return DataLinkExploreResponse(result=result, cache_hit=False)

    async def _call_datalink_explore(
        self,
        endpoint: str,
        arguments: dict[str, object],
    ) -> CallToolResult:
        """建立一次短生命周期 MCP 会话，绝不枚举或调用其他工具。"""

        async with streamable_http_client(endpoint) as streams:
            read_stream, write_stream, _get_session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("datalink_explore", arguments)
        if not isinstance(result, CallToolResult):
            raise ValueError("MCP tool did not return CallToolResult")
        return result


async def _forward_cancellation(
    token: QueryCancelToken,
    cancellation: CancellationSignal,
) -> None:
    """把 Graph 的同步取消信号传给现有 Gateway 查询取消令牌。"""

    while not cancellation.is_cancelled():
        await asyncio.sleep(0.05)
    token.cancel()


class _McpToolFailure(ValueError):
    """携带 DataLink 已约定错误码的 MCP 工具失败。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _parse_mcp_result(value: object) -> DataLinkExploreResult:
    """只接受 DataLink 的结构化结果或唯一 JSON 文本结果。"""

    if isinstance(value, Mapping):
        return DataLinkExploreResult.model_validate(value)
    if not isinstance(value, CallToolResult):
        raise ValueError("unexpected MCP result")
    if value.isError:
        text = _single_text(value)
        code = text.partition(":")[0].strip()
        raise _McpToolFailure(code)
    if isinstance(value.structuredContent, Mapping):
        return DataLinkExploreResult.model_validate(value.structuredContent)
    return DataLinkExploreResult.model_validate_json(_single_text(value))


def _single_text(result: CallToolResult) -> str:
    if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
        raise ValueError("MCP result must contain one text value")
    return result.content[0].text


def _map_tool_failure(code_value: str) -> AgentFailure:
    try:
        code = DataLinkErrorCode(code_value)
    except ValueError:
        return _datalink_invalid()
    if code is DataLinkErrorCode.DATALINK_UNAVAILABLE:
        return _datalink_unavailable()
    return _datalink_invalid()


def _cancelled_failure() -> AgentFailure:
    return AgentFailure(code=AgentErrorCode.RUN_CANCELED, message="分析已取消")


def _gateway_failure(message: str, *, retryable: bool = False) -> AgentFailure:
    return AgentFailure(
        code=AgentErrorCode.DATA_GATEWAY_FAILED,
        message=message,
        retryable=retryable,
    )


def _datalink_unavailable() -> AgentFailure:
    return AgentFailure(
        code=AgentErrorCode.DATALINK_UNAVAILABLE,
        message="DataLink 服务暂时不可用",
    )


def _datalink_invalid(message: str = "DataLink 请求或返回不符合约定") -> AgentFailure:
    return AgentFailure(code=AgentErrorCode.DATALINK_REQUEST_INVALID, message=message)
