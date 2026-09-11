from __future__ import annotations

import asyncio
import json

from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    DataLinkExploreResponse,
)
from agent_runtime.datalink_consumption import build_datalink_consumption
from contracts.datalink import DataLinkExploreResult, DataLinkSemanticContext
from contracts.status import DataSourceStatus, MessageRole
from contracts.validation import DATALINK_CONSUMPTION_PAYLOAD_LIMIT_BYTES, DataLinkConsumptionCreate
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import DataSourceModel, SessionModel
from metadata.repositories import MessageRepository, RunDatalinkConsumptionRepository, RunRepository
from runtime.run_event_pipeline import RunEventPipeline
from runtime.run_history_service import RunHistoryService


def test_consumption_builder_marks_too_large_without_truncating_json() -> None:
    name = "n" * 200
    description = "d" * 1000
    aliases = ["a" * 200] * 20
    nodes = [
        {
            "id": f"col_{index}",
            "type": "column",
            "name": name,
            "table": "orders",
            "description": description,
            "aliases": aliases,
        }
        for index in range(50)
    ]
    response = DataLinkExploreResponse(
        result=DataLinkExploreResult(
            datasource_id="ds",
            graph_version="graph_1",
            query="订单",
            nodes=nodes,
            is_truncated=True,
        ),
        cache_hit=False,
    )
    record = build_datalink_consumption(
        run_id="run_1",
        stage="prepare",
        query="订单",
        focus=None,
        max_nodes=12,
        schema_revision=1,
        graph_version="graph_1",
        result=response,
    )
    assert record.payload_status == "too_large"
    assert record.semantic_context is None
    assert record.summary.payload_bytes > DATALINK_CONSUMPTION_PAYLOAD_LIMIT_BYTES


def test_consumption_builder_records_unavailable() -> None:
    record = build_datalink_consumption(
        run_id="run_1",
        stage="tool",
        query="订单",
        focus="join_paths",
        max_nodes=8,
        schema_revision=1,
        graph_version=None,
        result=AgentFailure(code=AgentErrorCode.DATALINK_UNAVAILABLE, message="down"),
        tool_call_id="tool_1",
        consume_empty=True,
    )
    assert record.payload_status == "unavailable"
    assert record.consumer_receipt_status == "not_received"


def test_consumption_history_is_run_scoped_and_missing_for_old_runs(migrated_settings) -> None:
    async def exercise() -> None:
        engine = create_sqlite_engine(migrated_settings.metadata_database_url)
        factory = create_session_factory(engine)
        try:
            async with factory() as db:
                db.add(
                    DataSourceModel(
                        id="datasource_cons",
                        name="Cons",
                        type="csv",
                        source_ref="datasource_cons/source.csv",
                        file_size=1,
                        content_hash="hash",
                        schema_cache_json={"datasource_id": "datasource_cons", "tables": []},
                        schema_revision=1,
                        status=DataSourceStatus.READY.value,
                    )
                )
                db.add(
                    SessionModel(
                        id="session_cons", title="Cons", selected_datasource_id="datasource_cons"
                    )
                )
                await db.flush()
                message = await MessageRepository(db).create(
                    session_id="session_cons",
                    role=MessageRole.USER,
                    content_text="问题",
                )
                await RunRepository(db).create(
                    run_id="run_cons",
                    session_id="session_cons",
                    datasource_id="datasource_cons",
                    user_message_id=message.id,
                    question="问题",
                    idempotency_key="cons",
                    model_profile_id=None,
                    model_provider=None,
                    model_name=None,
                    schema_revision=1,
                    datalink_graph_version="graph_1",
                    run_timeout_seconds=60,
                    input_snapshot_ref="snapshots/run_cons/source.csv",
                    final_output_mode="markdown",
                    model_capability_fingerprint="a" * 64,
                )
                history = RunHistoryService(db)
                missing = await history.list_datalink_consumptions("run_cons")
                assert missing.historical_status == "missing"
                assert missing.items == []
                pipeline = RunEventPipeline(db)
                await pipeline.record_datalink_consumption(
                    DataLinkConsumptionCreate(
                        run_id="run_cons",
                        stage="prepare",
                        query="订单金额",
                        max_nodes=12,
                        schema_revision=1,
                        graph_version="graph_1",
                        mode="live",
                        payload_status="complete",
                        returned_status="ok",
                        consumer_receipt_status="received",
                        semantic_context=DataLinkSemanticContext(
                            mode="live",
                            graph_version="graph_1",
                        ),
                    )
                )
                await db.commit()
                listed = await history.list_datalink_consumptions("run_cons")
                assert listed.historical_status == "recorded"
                assert listed.items[0].query == "订单金额"
                assert listed.items[0].semantic_context is not None
                detail = await history.get_datalink_consumption("run_cons", listed.items[0].id)
                assert detail.seq == 1
                stored = await RunDatalinkConsumptionRepository(db).get(detail.id)
                assert stored is not None
                json.dumps(stored.payload_json)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
