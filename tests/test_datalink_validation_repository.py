from __future__ import annotations

import asyncio

from contracts.status import DataSourceStatus
from contracts.validation import DataLinkValidationRequest
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import DataSourceModel
from metadata.repositories import DataLinkValidationRepository


def test_validation_repository_idempotency_and_restart_recovery(migrated_settings) -> None:
    async def exercise() -> None:
        engine = create_sqlite_engine(migrated_settings.metadata_database_url)
        factory = create_session_factory(engine)
        try:
            async with factory() as db:
                db.add(
                    DataSourceModel(
                        id="datasource_validation",
                        name="Validation",
                        type="sqlite",
                        source_ref="datasource_validation/source.sqlite",
                        file_size=1,
                        content_hash="hash",
                        schema_revision=1,
                        status=DataSourceStatus.SCHEMA_READY.value,
                    )
                )
                await db.flush()
                repo = DataLinkValidationRepository(db)
                payload = DataLinkValidationRequest(
                    relation_id="rel_1",
                    graph_version="graph_1",
                    schema_revision=1,
                    idempotency_key="key-1",
                )
                created = await repo.create(
                    "datasource_validation",
                    payload,
                    endpoint_fingerprint="abc",
                    direction="source_to_target",
                )
                same = await repo.get_by_idempotency("datasource_validation", "key-1")
                assert same is not None and same.id == created.id
                assert created.status == "running"
                recovered = await repo.recover_interrupted()
                assert recovered == 1
                finished = await repo.get(created.id)
                assert finished is not None
                assert finished.status == "interrupted"
                assert finished.error_code == "PROCESS_RESTARTED"
                second = await repo.create(
                    "datasource_validation",
                    DataLinkValidationRequest(
                        relation_id="rel_2",
                        graph_version="graph_1",
                        schema_revision=1,
                        idempotency_key="key-2",
                    ),
                    endpoint_fingerprint="def",
                    direction="source_to_target",
                )
                await repo.request_cancel(second)
                ended = await repo.finish(
                    second, status="completed", metrics={"source_non_null_count": 1}
                )
                assert ended.status == "canceled"
                assert ended.error_code == "QUERY_CANCELED"
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
