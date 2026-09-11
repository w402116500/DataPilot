from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Lock

from contracts.datasources import ArtifactUsage, SchemaSummaryRead, SqlExecutionRead, TableDataRead
from contracts.errors import AppError, ErrorCode
from contracts.sensitive_fields import SensitiveFieldPolicy
from contracts.status import DataSourceStatus, DataSourceType
from metadata.models import DataSourceModel
from metadata.repositories import SqlAuditRepository

from data_gateway.exceptions import (
    DataSourceCheckError,
    QueryCanceledError,
    QueryFailedError,
    QueryTimeoutError,
    SqlGuardBlockedError,
)
from data_gateway.execute_errors import execute_failure_code, spec_for_code
from data_gateway.registry import AdapterRegistry
from data_gateway.serialization import serialize_table_result
from data_gateway.sql_guard import guard_sql
from data_gateway.types import (
    GatewaySourceSnapshot,
    QueryCancelToken,
    RelationalSourceAccess,
    SourceAccess,
    SourceHandle,
    SqlGuardResult,
    TableArtifactStore,
)


class DataGateway:
    """统一承接 Schema、预览与安全查询，避免其它模块直接读取源文件。"""

    def __init__(
        self,
        *,
        registry: AdapterRegistry,
        audits: SqlAuditRepository,
        artifacts: TableArtifactStore,
        policy: SensitiveFieldPolicy,
        default_query_limit: int,
        max_query_limit: int,
        query_timeout_seconds: int,
    ) -> None:
        self.registry = registry
        self.audits = audits
        self.artifacts = artifacts
        self.policy = policy
        self.default_query_limit = default_query_limit
        self.max_query_limit = max_query_limit
        self.query_timeout_seconds = query_timeout_seconds
        self._query_adapters: dict[tuple[str, int, int, str | None, str], object] = {}
        self._query_adapters_lock = Lock()

    async def inspect(
        self, source: DataSourceModel, source_path: Path | SourceAccess
    ) -> SchemaSummaryRead:
        """扫描受控源文件，生成首次检查和重试都会复用的 Schema 快照。"""

        adapter = self.registry.create(self._source_handle(source, source_path, schema=None))
        return await asyncio.to_thread(adapter.inspect_schema)

    async def preview(
        self,
        source: DataSourceModel,
        source_path: Path | SourceAccess,
        table_name: str,
        limit: int,
    ) -> TableDataRead:
        """预览已保存 Schema 中的表，并在返回前执行统一脱敏与安全序列化。"""

        schema = self._schema_for_ready_source(source)
        table_names = {table.name for table in schema.tables}
        if table_name not in table_names:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据表不在当前 Schema 白名单中",
                status_code=404,
            )
        adapter = self.registry.create(self._source_handle(source, source_path, schema=schema))
        raw = await asyncio.to_thread(adapter.preview_table, table_name, min(limit, 50))
        return serialize_table_result(raw, self._policy_for(source))

    async def run_sql_readonly(
        self,
        source: DataSourceModel | GatewaySourceSnapshot,
        source_path: Path | SourceAccess,
        sql: str,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        tool_call_id: str | None = None,
        repaired_from_audit_id: str | None = None,
        cancel_token: QueryCancelToken | None = None,
        timeout_seconds: float | None = None,
        max_rows: int | None = None,
        artifact_usage: ArtifactUsage = "query_result",
    ) -> SqlExecutionRead:
        """执行受控只读 SQL，并完整记录 Audit、脱敏结果和表格 Artifact。"""

        token = cancel_token or QueryCancelToken()
        schema = self._schema_for_ready_source(source)
        audit = await self.audits.create_proposed(
            datasource_id=source.id,
            schema_revision=source.schema_revision,
            connection_revision=source.connection_revision or 0,
            original_sql=sql,
            run_id=run_id,
            tool_call_id=tool_call_id,
            repaired_from_id=repaired_from_audit_id,
        )
        if token.is_cancelled():
            await self._mark_canceled(audit)
            raise QueryCanceledError("查询已取消")

        dialect = schema.dialect
        effective_max_limit = self.max_query_limit
        effective_default_limit = self.default_query_limit
        if max_rows is not None:
            effective_max_limit = min(effective_max_limit, max_rows)
            effective_default_limit = min(effective_default_limit, max_rows)
        guard = guard_sql(
            sql,
            dialect=dialect,
            schema=schema,
            default_limit=effective_default_limit,
            max_limit=effective_max_limit,
        )
        if not guard.allowed:
            await self.audits.update(
                audit,
                status="blocked",
                blocked_reason_code=guard.reason_code,
                blocked_reason=guard.reason,
            )
            issue = guard.issue
            if issue is None:
                raise RuntimeError("Guard 拒绝结果缺少问题说明")
            raise SqlGuardBlockedError(issue, audit.id)

        if token.is_cancelled():
            await self._mark_canceled(audit, guard=guard)
            raise QueryCanceledError("查询已取消")

        await self.audits.update(
            audit,
            status="running",
            normalized_sql=guard.normalized_sql,
            statement_type=guard.statement_type,
            referenced_tables_json=guard.referenced_tables,
        )
        effective_timeout_seconds = self.query_timeout_seconds
        if timeout_seconds is not None:
            effective_timeout_seconds = min(
                effective_timeout_seconds,
                max(1, int(timeout_seconds)),
            )
        # The query budget covers Adapter creation and source materialization as
        # well as SQL execution. CSV adapters may need to build a DuckDB snapshot
        # before execute() is reached, so starting the clock later hides the real
        # cost from both Audit and the caller's RunDeadline.
        started_at = time.monotonic()
        try:
            adapter = self._adapter_for_query(source, source_path, schema)
            raw = await asyncio.to_thread(
                adapter.run_sql,
                guard.normalized_sql or sql,
                effective_timeout_seconds,
                token,
            )
        except QueryCanceledError:
            await self._mark_canceled(
                audit,
                guard=guard,
                elapsed_ms=_elapsed_ms(started_at),
            )
            raise
        except QueryTimeoutError as exc:
            await self.audits.update(
                audit,
                status="timeout",
                elapsed_ms=_elapsed_ms(started_at),
                error_code="QUERY_TIMEOUT",
                error_message="查询超时",
            )
            raise QueryTimeoutError("查询超时", audit.id) from exc
        except DataSourceCheckError as exc:
            error_code = execute_failure_code(exc.code)
            await self.audits.update(
                audit,
                status="failed",
                elapsed_ms=_elapsed_ms(started_at),
                error_code=error_code,
                error_message=exc.message,
            )
            raise QueryFailedError(
                exc.message,
                audit.id,
                code=error_code,
                hint=_execute_failure_hint(exc),
                retryable=exc.retryable,
            ) from None
        except Exception:
            await self.audits.update(
                audit,
                status="failed",
                elapsed_ms=_elapsed_ms(started_at),
                error_code="QUERY_FAILED",
                error_message="查询执行失败",
            )
            raise QueryFailedError("查询执行失败", audit.id, retryable=False) from None

        if token.is_cancelled():
            await self._mark_canceled(audit, guard=guard, elapsed_ms=_elapsed_ms(started_at))
            raise QueryCanceledError("查询已取消")
        result = serialize_table_result(raw, self._policy_for(source))
        elapsed_ms = _elapsed_ms(started_at)
        try:
            artifact_id = await self.artifacts.create_table_artifact(
                result=result,
                title="查询结果",
                run_id=run_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                artifact_usage=artifact_usage,
            )
        except Exception:
            await self.audits.update(
                audit,
                status="failed",
                elapsed_ms=elapsed_ms,
                error_code="ARTIFACT_PERSISTENCE_FAILED",
                error_message="表格结果保存失败",
            )
            raise

        await self.audits.update(
            audit,
            status="succeeded",
            artifact_id=artifact_id,
            row_count=result.row_count,
            elapsed_ms=elapsed_ms,
        )
        return SqlExecutionRead(
            **result.model_dump(),
            audit_log_id=audit.id,
            artifact_id=artifact_id,
            elapsed_ms=elapsed_ms,
        )

    def _adapter_for_query(
        self,
        source: DataSourceModel,
        source_path: Path | SourceAccess,
        schema: SchemaSummaryRead,
    ) -> object:
        """按数据源版本复用查询 Adapter，隔离不同文件和 Schema revision。"""

        if isinstance(source_path, RelationalSourceAccess):
            return self.registry.create(self._source_handle(source, source_path, schema=schema))
        key = (
            source.id,
            source.schema_revision,
            source.connection_revision or 0,
            source.content_hash,
            str(source_path),
        )
        with self._query_adapters_lock:
            adapter = self._query_adapters.get(key)
            if adapter is None:
                adapter = self.registry.create(
                    self._source_handle(source, source_path, schema=schema)
                )
                self._query_adapters[key] = adapter
            return adapter

    async def _mark_canceled(
        self,
        audit,
        *,
        guard: SqlGuardResult | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        """将同一条尝试标记为 canceled；取消不能生成结果文件或第二条 Audit。"""

        changes: dict[str, object] = {
            "status": "canceled",
            "error_code": "QUERY_CANCELED",
            "error_message": "查询已取消",
        }
        if guard is not None:
            changes.update(
                normalized_sql=guard.normalized_sql,
                statement_type=guard.statement_type,
                referenced_tables_json=guard.referenced_tables,
            )
        if elapsed_ms is not None:
            changes["elapsed_ms"] = elapsed_ms
        await self.audits.update(audit, **changes)

    def _schema_for_ready_source(self, source: DataSourceModel) -> SchemaSummaryRead:
        """只允许 Schema 已固定的数据源读取，拒绝检查、建图和删除中的不稳定状态。"""

        if (
            source.status
            not in {
                DataSourceStatus.SCHEMA_READY.value,
                DataSourceStatus.READY.value,
            }
            or source.schema_cache_json is None
        ):
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源尚未完成检查，暂时不能读取",
                status_code=409,
            )
        return SchemaSummaryRead.model_validate(source.schema_cache_json)

    @staticmethod
    def _policy_for(source: DataSourceModel) -> SensitiveFieldPolicy:
        """每个查询都读取 DataSource 已保存的显式遮蔽配置快照。"""

        return SensitiveFieldPolicy(
            mask_fields=frozenset(source.mask_fields_json or []),
            confirmed=source.mask_fields_confirmed,
        )

    def _source_handle(
        self,
        source: DataSourceModel,
        source_path: Path | SourceAccess,
        *,
        schema: SchemaSummaryRead | None,
    ) -> SourceHandle:
        """从已验证的 Metadata 组装 Adapter 输入，不让 Adapter 自行猜测类型或路径。"""

        try:
            datasource_type = DataSourceType(source.type)
        except ValueError as exc:
            raise DataSourceCheckError(
                "DATASOURCE_CHECK_FAILED",
                "数据源类型无效",
                retryable=False,
            ) from exc
        return SourceHandle(
            datasource_id=source.id,
            datasource_type=datasource_type,
            source_path=source_path if isinstance(source_path, Path) else None,
            access=source_path if not isinstance(source_path, Path) else None,
            connection_revision=source.connection_revision or 0,
            schema=schema,
        )


def _execute_failure_hint(exc: DataSourceCheckError) -> str | None:
    """Reuse the Gateway error table when Adapter already chose a stable code."""

    if execute_failure_code(exc.code) != exc.code:
        return None
    if exc.hint:
        return exc.hint
    spec = spec_for_code(exc.code)
    return spec.hint if spec is not None else None


def _elapsed_ms(started_at: float) -> int:
    """基于单调时钟计算耗时，避免系统时间调整导致审计记录异常。"""

    return max(0, round((time.monotonic() - started_at) * 1_000))
