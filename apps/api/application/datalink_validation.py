from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any

from agent_runtime.ports import CancellationSignal
from contracts.datalink import DataLinkEdgeType, DataLinkNodeType, DataLinkRelationRead
from contracts.datasources import DataSourceRead, SchemaSummaryRead, SqlExecutionRead
from contracts.errors import AppError, ErrorCode
from contracts.validation import DataLinkValidationRead, DataLinkValidationRequest
from data_gateway.exceptions import QueryCanceledError, QueryTimeoutError, SqlGuardBlockedError
from data_gateway.types import QueryCancelToken
from metadata.models import DataLinkValidationModel
from metadata.repositories import DataLinkValidationRepository
from runtime.run_cancel_registry import RunCancellation
from runtime.validation_cancel_registry import ValidationCancelRegistry
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from application.datalink_client import DataLinkClientError
from application.datalink_validation_sql import plan_relation_validation
from application.datasources import DataSourceService


class DataLinkValidationService:
    """Owns relation validation tasks; SQL still goes through Data Gateway."""

    def __init__(
        self,
        datasources: DataSourceService,
        repository: DataLinkValidationRepository,
        *,
        db: AsyncSession,
        cancels: ValidationCancelRegistry,
        query_timeout_seconds: int,
    ) -> None:
        self.datasources = datasources
        self.repository = repository
        self.db = db
        self.cancels = cancels
        self.query_timeout_seconds = query_timeout_seconds

    async def create(
        self,
        datasource_id: str,
        payload: DataLinkValidationRequest,
        cancellation: CancellationSignal | None = None,
    ) -> DataLinkValidationRead:
        source = await self.datasources.get_readable(datasource_id)
        schema = _require_schema(source)
        _require_current_versions(source, payload)
        existing = await self.repository.get_by_idempotency(datasource_id, payload.idempotency_key)
        if existing is not None:
            return _read(existing, source)
        relation = await self._load_relation(datasource_id, payload)
        plan = plan_relation_validation(
            schema,
            source_table=_require_column_table(relation.source.table),
            source_column=relation.source.name,
            target_table=_require_column_table(relation.target.table),
            target_column=relation.target.name,
        )
        try:
            model = await self.repository.create(
                datasource_id,
                payload,
                endpoint_fingerprint=plan.endpoint_fingerprint,
                direction=plan.direction,
            )
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            existing = await self.repository.get_by_idempotency(
                datasource_id, payload.idempotency_key
            )
            if existing is None:
                raise
            return _read(existing, await self.datasources.get_readable(datasource_id))
        return await self._execute(model, plan, cancellation or RunCancellation())

    async def get(self, datasource_id: str, validation_id: str) -> DataLinkValidationRead:
        model = await self._require(datasource_id, validation_id)
        source = await self.datasources.get(datasource_id)
        return _read(model, source)

    async def list(self, datasource_id: str) -> list[DataLinkValidationRead]:
        source = await self.datasources.get_readable(datasource_id)
        return [
            _read(item, source) for item in await self.repository.list_for_datasource(datasource_id)
        ]

    async def cancel(self, datasource_id: str, validation_id: str) -> DataLinkValidationRead:
        model = await self._require(datasource_id, validation_id)
        source = await self.datasources.get_readable(datasource_id)
        if model.status != "running":
            return _read(model, source)
        await self.repository.request_cancel(model)
        self.cancels.cancel(model.id)
        await self.db.commit()
        refreshed = await self.repository.get(model.id)
        assert refreshed is not None
        return _read(refreshed, source)

    async def recover_interrupted(self) -> int:
        return await self.repository.recover_interrupted()

    async def _require(self, datasource_id: str, validation_id: str) -> DataLinkValidationModel:
        model = await self.repository.get(validation_id)
        if model is None or model.datasource_id != datasource_id:
            raise AppError(ErrorCode.VALIDATION_NOT_FOUND, "核验任务不存在", status_code=404)
        return model

    async def _finish(
        self,
        model: DataLinkValidationModel,
        *,
        status: str,
        metrics: dict[str, Any],
        audit_ids: list[str],
        artifact_ids: list[str],
        error_code: str | None = None,
    ) -> DataLinkValidationModel:
        current = await self.repository.get(model.id) or model
        return await self.repository.finish(
            current,
            status=status,
            metrics=metrics or None,
            audit_log_ids=audit_ids,
            artifact_ids=artifact_ids,
            error_code=error_code,
        )

    async def _load_relation(
        self, datasource_id: str, payload: DataLinkValidationRequest
    ) -> DataLinkRelationRead:
        try:
            detail = await self.datasources.datalink_client.relation(
                datasource_id,
                payload.relation_id,
                graph_version=payload.graph_version,
            )
        except DataLinkClientError as exc:
            if exc.code.value in {"INVALID_QUERY", "GRAPH_VERSION_NOT_FOUND"}:
                raise AppError(
                    ErrorCode.RELATION_NOT_FOUND,
                    "关系不存在或当前图谱版本不可核验",
                    status_code=404,
                ) from exc
            raise DataSourceService._to_app_error(exc) from exc
        relation = detail.item
        if (
            relation.type not in {DataLinkEdgeType.FOREIGN_KEY, DataLinkEdgeType.JOINABLE}
            or relation.source.type != DataLinkNodeType.COLUMN
            or relation.target.type != DataLinkNodeType.COLUMN
            or not relation.source.table
            or not relation.target.table
        ):
            raise AppError(
                ErrorCode.RELATION_NOT_VALIDATABLE,
                "只有同源单列等值关系可以核验",
                status_code=409,
            )
        return relation

    async def _execute(
        self,
        model: DataLinkValidationModel,
        plan,
        cancellation: CancellationSignal,
    ) -> DataLinkValidationRead:
        signal = self.cancels.register(model.id, _as_run_cancellation(cancellation))
        query_token = QueryCancelToken()
        watcher = asyncio.create_task(_forward_cancellation(query_token, signal))
        metrics: dict[str, Any] = {}
        audit_ids: list[str] = []
        artifact_ids: list[str] = []
        started = time.monotonic()
        try:
            for name, sql in plan.statements:
                model = await self.repository.get(model.id) or model
                if model.cancel_requested or signal.is_cancelled():
                    raise QueryCanceledError("查询已取消")
                remaining = self.query_timeout_seconds - (time.monotonic() - started)
                if remaining < 1:
                    raise QueryTimeoutError("查询超时")
                result = await self.datasources.run_readonly_sql(
                    datasource_id=model.datasource_id,
                    sql=sql,
                    cancel_token=query_token,
                    timeout_seconds=remaining,
                    expected_schema_revision=model.schema_revision,
                )
                audit_ids.append(result.audit_log_id)
                if result.artifact_id:
                    artifact_ids.append(result.artifact_id)
                _merge_metrics(metrics, name, result)
                await self.db.commit()
            model = await self._finish(
                model,
                status="completed",
                metrics=metrics,
                audit_ids=audit_ids,
                artifact_ids=artifact_ids,
            )
        except QueryCanceledError:
            model = await self._finish(
                model,
                status="canceled",
                metrics=metrics,
                audit_ids=audit_ids,
                artifact_ids=artifact_ids,
                error_code="QUERY_CANCELED",
            )
        except QueryTimeoutError:
            model = await self._finish(
                model,
                status="partial" if metrics else "failed",
                metrics=metrics,
                audit_ids=audit_ids,
                artifact_ids=artifact_ids,
                error_code="QUERY_TIMEOUT",
            )
        except SqlGuardBlockedError as exc:
            audit_ids.append(exc.audit_log_id)
            model = await self._finish(
                model,
                status="failed",
                metrics=metrics,
                audit_ids=audit_ids,
                artifact_ids=artifact_ids,
                error_code=exc.code,
            )
        except AppError as exc:
            model = await self._finish(
                model,
                status="failed",
                metrics=metrics,
                audit_ids=audit_ids,
                artifact_ids=artifact_ids,
                error_code=exc.code.value,
            )
            await self.db.commit()
            if exc.code in {ErrorCode.HEAD_STALE, ErrorCode.DATASOURCE_NOT_READY}:
                raise
            source = await self.datasources.get(model.datasource_id)
            return _read(model, source)
        except Exception:
            model = await self._finish(
                model,
                status="failed",
                metrics=metrics,
                audit_ids=audit_ids,
                artifact_ids=artifact_ids,
                error_code="QUERY_FAILED",
            )
        finally:
            query_token.cancel()
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            self.cancels.unregister(model.id)
        await self.db.commit()
        source = await self.datasources.get(model.datasource_id)
        return _read(model, source)


def _as_run_cancellation(cancellation: CancellationSignal) -> RunCancellation:
    if isinstance(cancellation, RunCancellation):
        return cancellation
    signal = RunCancellation()
    if cancellation.is_cancelled():
        signal.cancel()
    return signal


async def _forward_cancellation(token: QueryCancelToken, cancellation: CancellationSignal) -> None:
    while not cancellation.is_cancelled():
        await asyncio.sleep(0.05)
    token.cancel()


def _require_schema(source: DataSourceRead) -> SchemaSummaryRead:
    if source.schema_summary is None:
        raise AppError(ErrorCode.DATASOURCE_NOT_READY, "数据源尚未生成可用 Schema", status_code=409)
    return source.schema_summary


def _require_current_versions(source: DataSourceRead, payload: DataLinkValidationRequest) -> None:
    if (
        source.schema_revision != payload.schema_revision
        or source.datalink_graph_version != payload.graph_version
    ):
        raise AppError(ErrorCode.HEAD_STALE, "数据源版本已变化，请刷新后重试", status_code=409)


def _require_column_table(table: str | None) -> str:
    if not table:
        raise AppError(
            ErrorCode.RELATION_NOT_VALIDATABLE,
            "只有同源单列等值关系可以核验",
            status_code=409,
        )
    return table


def _merge_metrics(metrics: dict[str, Any], name: str, result: SqlExecutionRead) -> None:
    row = _row_map(result)
    if name == "source_stats":
        metrics["source_non_null_count"] = _as_int(row.get("source_non_null_count"))
        metrics["source_distinct_count"] = _as_int(row.get("source_distinct_count"))
        return
    if name == "target_stats":
        metrics["target_non_null_count"] = _as_int(row.get("target_non_null_count"))
        metrics["target_distinct_count"] = _as_int(row.get("target_distinct_count"))
        return
    if name == "target_duplicates":
        metrics["target_duplicate_count"] = _as_int(row.get("target_duplicate_count"))
        return
    if name == "source_unmatched":
        metrics["source_unmatched_count"] = _as_int(row.get("source_unmatched_count"))
        return
    metrics["multiple_match_risk"] = _as_int(row.get("multiple_match_count")) > 0


def _row_map(result: SqlExecutionRead) -> dict[str, Any]:
    if not result.rows:
        return {}
    return dict(zip(result.columns, result.rows[0], strict=False))


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _read(model: DataLinkValidationModel, source: DataSourceRead) -> DataLinkValidationRead:
    metrics = model.metrics_json or {}
    expired = (
        source.schema_revision != model.schema_revision
        or source.datalink_graph_version != model.graph_version
    )
    return DataLinkValidationRead(
        id=model.id,
        datasource_id=model.datasource_id,
        relation_id=model.relation_id,
        graph_version=model.graph_version,
        schema_revision=model.schema_revision,
        status=model.status,  # type: ignore[arg-type]
        source_non_null_count=metrics.get("source_non_null_count"),
        target_non_null_count=metrics.get("target_non_null_count"),
        source_distinct_count=metrics.get("source_distinct_count"),
        target_distinct_count=metrics.get("target_distinct_count"),
        target_duplicate_count=metrics.get("target_duplicate_count"),
        source_unmatched_count=metrics.get("source_unmatched_count"),
        multiple_match_risk=metrics.get("multiple_match_risk"),
        direction=model.direction,  # type: ignore[arg-type]
        endpoint_fingerprint=model.endpoint_fingerprint,
        audit_log_ids=list(model.audit_log_ids_json or []),
        artifact_ids=list(model.artifact_ids_json or []),
        error_code=model.error_code,
        expired=expired,
        created_at=model.created_at,
        finished_at=model.finished_at,
    )
