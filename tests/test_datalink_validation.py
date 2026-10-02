from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from application.datalink_client import DataLinkClientError
from application.datalink_validation_sql import plan_relation_validation
from contracts.datalink import (
    DataLinkBrowserNodeRead,
    DataLinkBuildRead,
    DataLinkBuildStatus,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkGraphRead,
    DataLinkHealthRead,
    DataLinkNodeType,
    DataLinkRebuildRequest,
    DataLinkRebuildResult,
    DataLinkRelationDetailRead,
    DataLinkRelationRead,
    DataLinkRemoveResult,
    DataLinkStatusRead,
)
from contracts.datasources import SchemaColumnRead, SchemaSummaryRead, SchemaTableRead
from contracts.errors import AppError, ErrorCode
from contracts.validation import DataLinkValidationRequest
from fastapi.testclient import TestClient
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import DataSourceModel
from metadata.repositories import DataLinkValidationRepository, DataSourceRepository
from server.app import create_app


def unwrap(response):
    body = response.json()
    assert body["error"] is None, body
    return body["data"]


def _column(table: str, name: str) -> DataLinkBrowserNodeRead:
    return DataLinkBrowserNodeRead(
        id=f"{table}.{name}",
        type=DataLinkNodeType.COLUMN,
        name=name,
        table=table,
    )


def _relation() -> DataLinkRelationRead:
    return DataLinkRelationRead(
        id="rel_orders_customer",
        source=_column("orders", "customer_id"),
        target=_column("customers", "id"),
        type=DataLinkEdgeType.FOREIGN_KEY,
        confidence=1,
        evidence=None,
        provenance="database_foreign_key",
        enabled=True,
        join_eligible=True,
    )


class RelationClient:
    def __init__(
        self,
        relation: DataLinkRelationRead | list[DataLinkRelationRead],
        graph_version: str,
    ) -> None:
        self._builds: dict[str, DataLinkBuildRead] = {}
        relations = [relation] if isinstance(relation, DataLinkRelationRead) else relation
        self._relations = {item.id: item for item in relations}
        self._graph_version = graph_version

    async def health(self) -> DataLinkHealthRead:
        return DataLinkHealthRead(
            status="healthy",
            graph_database="ok",
            source_root="ok",
            model_configured=True,
            mcp_mounted=True,
        )

    async def rebuild(self, payload: DataLinkRebuildRequest) -> DataLinkRebuildResult:
        build = DataLinkBuildRead(
            build_id=f"build_{payload.datasource_id}_{payload.schema_revision}",
            datasource_id=payload.datasource_id,
            schema_revision=payload.schema_revision,
            attempt_no=1,
            status=DataLinkBuildStatus.COMPLETED,
            graph_version=f"graph_{payload.datasource_id}_{payload.schema_revision}",
            created_at=datetime.now(UTC),
        )
        self._builds[payload.datasource_id] = build
        return DataLinkRebuildResult(
            build_id=build.build_id,
            datasource_id=build.datasource_id,
            status=build.status,
            requested_schema_revision=build.schema_revision,
            graph_version=build.graph_version,
        )

    async def status(self, datasource_id: str) -> DataLinkStatusRead:
        build = self._builds.get(datasource_id)
        return DataLinkStatusRead(
            datasource_id=datasource_id,
            current_graph_version=build.graph_version if build else None,
            current_build=build,
        )

    async def graph(
        self, datasource_id: str, *, graph_version: str | None = None
    ) -> DataLinkGraphRead:
        build = self._builds.get(datasource_id)
        return DataLinkGraphRead(
            datasource_id=datasource_id,
            graph_version=graph_version or (build.graph_version if build else None),
        )

    async def remove(self, datasource_id: str) -> DataLinkRemoveResult:
        return DataLinkRemoveResult(
            datasource_id=datasource_id,
            removed=self._builds.pop(datasource_id, None) is not None,
        )

    async def relation(self, datasource_id: str, relation_id: str, *, graph_version: str):
        item = self._relations.get(relation_id)
        if item is None or graph_version != self._graph_version:
            raise DataLinkClientError(
                DataLinkErrorCode.INVALID_QUERY, "关系不存在", status_code=409
            )
        return DataLinkRelationDetailRead(
            datasource_id=datasource_id,
            graph_version=graph_version,
            item=item,
        )


def test_validation_sql_rejects_incompatible_types() -> None:
    schema = SchemaSummaryRead(
        datasource_id="ds",
        dialect="sqlite",
        tables=[
            SchemaTableRead(
                name="orders",
                columns=[SchemaColumnRead(name="customer_id", type="INTEGER", nullable=True)],
            ),
            SchemaTableRead(
                name="customers",
                columns=[SchemaColumnRead(name="id", type="TEXT", nullable=True)],
            ),
        ],
    )
    with pytest.raises(AppError) as exc:
        plan_relation_validation(
            schema,
            source_table="orders",
            source_column="customer_id",
            target_table="customers",
            target_column="id",
        )
    assert exc.value.code is ErrorCode.SCHEMA_TYPE_INCOMPATIBLE


def test_validation_sql_quotes_identifiers() -> None:
    schema = SchemaSummaryRead(
        datasource_id="ds",
        dialect="sqlite",
        tables=[
            SchemaTableRead(
                name="order items",
                columns=[SchemaColumnRead(name="parent id", type="INTEGER", nullable=True)],
            ),
            SchemaTableRead(
                name="parents",
                columns=[SchemaColumnRead(name="id", type="INTEGER", nullable=True)],
            ),
        ],
    )
    plan = plan_relation_validation(
        schema,
        source_table="order items",
        source_column="parent id",
        target_table="parents",
        target_column="id",
    )
    assert all('"order items"' in sql or '"parents"' in sql for _, sql in plan.statements)
    assert any('"parent id"' in sql for _, sql in plan.statements)
    assert plan.endpoint_fingerprint


def _prepare_sqlite_source(client, tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "relations.sqlite"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE customers (id INTEGER, name TEXT);
            CREATE TABLE orders (id INTEGER, customer_id INTEGER);
            INSERT INTO customers (id, name) VALUES (1, 'a'), (2, 'b'), (2, 'c'), (NULL, 'n');
            INSERT INTO orders (id, customer_id) VALUES (1, 1), (2, 2), (3, 3), (4, NULL);
            CREATE TABLE parents (id INTEGER);
            CREATE TABLE children (parent_id INTEGER);
            INSERT INTO parents (id) VALUES (1), (2);
            INSERT INTO children (parent_id) VALUES (1), (1), (2);
            """
        )
    with source.open("rb") as handle:
        uploaded = unwrap(
            client.post(
                "/datasources/upload",
                data={"type": "sqlite", "name": "Relations"},
                files={"file": ("relations.sqlite", handle, "application/octet-stream")},
            )
        )
    datasource_id = uploaded["id"]
    detail = unwrap(client.get(f"/datasources/{datasource_id}"))
    unwrap(client.patch(f"/datasources/{datasource_id}/mask-fields", json={"mask_fields": []}))
    return detail


def test_relation_validation_metrics_audit_and_idempotency(
    migrated_settings, tmp_path: Path
) -> None:
    relation = _relation()
    many_to_one = DataLinkRelationRead(
        id="rel_children_parent",
        source=_column("children", "parent_id"),
        target=_column("parents", "id"),
        type=DataLinkEdgeType.FOREIGN_KEY,
        confidence=1,
        evidence=None,
        provenance="database_foreign_key",
        enabled=True,
        join_eligible=True,
    )
    app = create_app(
        migrated_settings,
        datalink_client=RelationClient([relation, many_to_one], "graph_1"),
    )
    with TestClient(app) as client:
        detail = _prepare_sqlite_source(client, tmp_path)
        datasource_id = detail["id"]

        async def set_graph() -> None:
            engine = create_sqlite_engine(migrated_settings.metadata_database_url)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    model = await DataSourceRepository(session).get(datasource_id)
                    assert model is not None
                    model.datalink_graph_version = "graph_1"
                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(set_graph())
        payload = {
            "relation_id": relation.id,
            "graph_version": "graph_1",
            "schema_revision": detail["schema_revision"],
            "idempotency_key": "validate-1",
        }
        created = unwrap(
            client.post(f"/datasources/{datasource_id}/datalink/validations", json=payload)
        )
        assert created["status"] == "completed"
        assert created["source_non_null_count"] == 3
        assert created["source_distinct_count"] == 3
        assert created["target_non_null_count"] == 3
        assert created["target_distinct_count"] == 2
        assert created["target_duplicate_count"] == 1
        assert created["source_unmatched_count"] == 1
        assert created["multiple_match_risk"] is True
        assert created["audit_log_ids"]
        assert created["artifact_ids"]
        assert len(created["audit_log_ids"]) == len(created["artifact_ids"])
        replay = unwrap(
            client.post(f"/datasources/{datasource_id}/datalink/validations", json=payload)
        )
        assert replay["id"] == created["id"]
        listed = unwrap(client.get(f"/datasources/{datasource_id}/datalink/validations"))
        assert listed[0]["id"] == created["id"]
        fetched = unwrap(
            client.get(f"/datasources/{datasource_id}/datalink/validations/{created['id']}")
        )
        assert fetched["endpoint_fingerprint"] == created["endpoint_fingerprint"]

        safe = unwrap(
            client.post(
                f"/datasources/{datasource_id}/datalink/validations",
                json={
                    "relation_id": many_to_one.id,
                    "graph_version": "graph_1",
                    "schema_revision": detail["schema_revision"],
                    "idempotency_key": "validate-many-to-one",
                },
            )
        )
        assert safe["status"] == "completed"
        assert safe["multiple_match_risk"] is False
        assert safe["source_unmatched_count"] == 0

        stale = client.post(
            f"/datasources/{datasource_id}/datalink/validations",
            json={**payload, "idempotency_key": "validate-2", "schema_revision": 99},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "HEAD_STALE"


def test_validation_timeout_after_partial_metrics_is_not_completed(migrated_settings) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from application.datalink_validation import DataLinkValidationService
    from contracts.datasources import DataSourceRead, SqlExecutionRead
    from contracts.status import DataSourceStatus
    from data_gateway.exceptions import QueryTimeoutError
    from runtime.run_cancel_registry import RunCancellation
    from runtime.validation_cancel_registry import ValidationCancelRegistry

    async def exercise() -> str:
        engine = create_sqlite_engine(migrated_settings.metadata_database_url)
        factory = create_session_factory(engine)
        try:
            async with factory() as db:
                db.add(
                    DataSourceModel(
                        id="datasource_timeout",
                        name="Timeout",
                        type="sqlite",
                        source_ref="datasource_timeout/source.sqlite",
                        file_size=1,
                        content_hash="hash",
                        schema_revision=1,
                        status=DataSourceStatus.SCHEMA_READY.value,
                    )
                )
                await db.flush()
                repo = DataLinkValidationRepository(db)
                model = await repo.create(
                    "datasource_timeout",
                    DataLinkValidationRequest(
                        relation_id="rel",
                        graph_version="graph",
                        schema_revision=1,
                        idempotency_key="timeout-key",
                    ),
                    endpoint_fingerprint="fp",
                    direction="source_to_target",
                )
                await db.commit()
                calls = {"n": 0}

                async def run_sql(**_kwargs):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return SqlExecutionRead(
                            columns=["source_non_null_count", "source_distinct_count"],
                            rows=[[3, 3]],
                            row_count=1,
                            audit_log_id="audit_timeout_1",
                            artifact_id="art_timeout_1",
                            elapsed_ms=1,
                        )
                    raise QueryTimeoutError("查询超时")

                datasources = SimpleNamespace(
                    run_readonly_sql=run_sql,
                    get=AsyncMock(
                        return_value=DataSourceRead(
                            id="datasource_timeout",
                            name="Timeout",
                            description=None,
                            type="sqlite",
                            status=DataSourceStatus.SCHEMA_READY,
                            schema_revision=1,
                            mask_fields=[],
                            mask_fields_confirmed=True,
                            schema_summary=None,
                            datalink_build_id=None,
                            datalink_graph_version="graph",
                            last_error_code=None,
                            last_error_message=None,
                            last_test_at=None,
                            created_at=datetime.now(UTC),
                            updated_at=datetime.now(UTC),
                        )
                    ),
                )
                service = DataLinkValidationService(
                    datasources,  # type: ignore[arg-type]
                    repo,
                    db=db,
                    cancels=ValidationCancelRegistry(),
                    query_timeout_seconds=15,
                )
                plan = SimpleNamespace(
                    statements=(
                        ("source_stats", "SELECT 1"),
                        ("target_stats", "SELECT 2"),
                    )
                )
                result = await service._execute(model, plan, RunCancellation())
                return result.status + ":" + str(result.error_code)
        finally:
            await engine.dispose()

    assert asyncio.run(exercise()) == "partial:QUERY_TIMEOUT"


def test_validation_startup_marks_running_as_interrupted(migrated_settings) -> None:
    async def seed() -> str:
        engine = create_sqlite_engine(migrated_settings.metadata_database_url)
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                session.add(
                    DataSourceModel(
                        id="datasource_interrupted_validation",
                        name="Interrupted",
                        type="csv",
                        source_ref="datasource_interrupted_validation/source.csv",
                        file_size=1,
                        content_hash="hash",
                        schema_revision=1,
                        status="schema_ready",
                    )
                )
                from contracts.validation import DataLinkValidationRequest

                await session.flush()
                model = await DataLinkValidationRepository(session).create(
                    "datasource_interrupted_validation",
                    DataLinkValidationRequest(
                        relation_id="rel",
                        graph_version="graph",
                        schema_revision=1,
                        idempotency_key="k",
                    ),
                    endpoint_fingerprint="fp",
                    direction="source_to_target",
                )
                await session.commit()
                return model.id
        finally:
            await engine.dispose()

    validation_id = asyncio.run(seed())
    with TestClient(
        create_app(migrated_settings, datalink_client=RelationClient(_relation(), "graph"))
    ):
        pass

    async def read() -> str:
        engine = create_sqlite_engine(migrated_settings.metadata_database_url)
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                model = await DataLinkValidationRepository(session).get(validation_id)
                assert model is not None
                return model.status
        finally:
            await engine.dispose()

    assert asyncio.run(read()) == "interrupted"
