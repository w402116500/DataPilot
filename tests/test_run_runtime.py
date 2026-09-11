from __future__ import annotations

import asyncio
import json

import pytest
from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    AnalysisClaimAuditFact,
    AnalysisClaimAuditSummary,
    AnalysisClarificationDraft,
    AnalysisOutcome,
    RunContext,
)
from application.run_execution import RunExecutionContext
from application.run_runtime import RunRuntimeError
from contracts.run_events import RunEventCreate, RunEventType
from contracts.runs import ModelRuntimeSnapshot
from contracts.status import DataSourceStatus, MessageRole, RunStatus
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import (
    DatasourceConversationStateModel,
    DataSourceModel,
    MessageModel,
    RunEventModel,
    RunModel,
    SessionModel,
    ToolCallModel,
)
from metadata.repositories import MessageRepository, RunRepository, SqlAuditRepository
from runtime.run_cancel_registry import RunCancelRegistry
from runtime.run_event_pipeline import RunEventNotifier, RunEventPipeline, _validate_payload
from runtime.run_executor import RunExecutor
from runtime.run_finalizer import RunFinalizer, _answer_data_freshness, _historical_topic
from sqlalchemy import select


async def _seed_run(settings) -> None:
    engine = create_sqlite_engine(settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            db.add(
                DataSourceModel(
                    id="datasource_runtime",
                    name="Runtime",
                    description=None,
                    type="csv",
                    source_ref="datasource_runtime/source.csv",
                    file_size=1,
                    content_hash="hash",
                    schema_cache_json={"datasource_id": "datasource_runtime", "tables": []},
                    schema_revision=1,
                    status=DataSourceStatus.READY.value,
                )
            )
            session = SessionModel(
                id="session_runtime",
                title="Runtime",
                selected_datasource_id="datasource_runtime",
            )
            db.add(session)
            await db.flush()
            message = await MessageRepository(db).create(
                session_id=session.id,
                role=MessageRole.USER,
                content_text="问题",
            )
            await RunRepository(db).create(
                run_id="run_runtime",
                session_id=session.id,
                datasource_id="datasource_runtime",
                user_message_id=message.id,
                question="问题",
                idempotency_key="key_runtime",
                model_profile_id=None,
                model_provider=None,
                model_name=None,
                schema_revision=1,
                datalink_graph_version=None,
                run_timeout_seconds=60,
                input_snapshot_ref="snapshots/run_runtime/source.csv",
                final_output_mode="json_schema",
                model_capability_fingerprint="a" * 64,
            )
            run = await db.get(RunModel, "run_runtime")
            assert run is not None
            run.protocol_id = "data-analysis"
            await db.commit()
    finally:
        await engine.dispose()


class _TimeoutRuntime:
    """仅用于验证执行器的全局时限收尾，不访问模型或数据源。"""

    async def execute(self, _execution, *, cancellation):
        while not cancellation.is_cancelled():
            await asyncio.sleep(0.01)
        raise RunRuntimeError(AgentFailure(code=AgentErrorCode.RUN_CANCELED, message="分析已取消"))


def _execution_context(*, timeout_seconds: int) -> RunExecutionContext:
    return RunExecutionContext(
        run_context=RunContext(
            run_id="run_runtime",
            session_id="session_runtime",
            datasource_id="datasource_runtime",
            model_profile_id="profile_runtime",
            model_name="demo-model",
            schema_revision=1,
            input_snapshot_ref="snapshots/run_runtime/source.csv",
            input_filename="source.csv",
            question="问题",
        ),
        model=ModelRuntimeSnapshot(
            profile_id="profile_runtime",
            provider="openai-compatible",
            model_name="demo-model",
            base_url="https://model.example/v1",
            temperature=0,
            run_timeout_seconds=timeout_seconds,
            final_output_mode="json_schema",
            model_capability_fingerprint="a" * 64,
        ),
        api_key="test-api-key",
        sandbox_image="datapilot-test:latest",
        input_snapshot_path="snapshots/run_runtime/source.csv",
    )


def test_answer_data_freshness_requires_a_passed_claim() -> None:
    failed_claim = AnalysisClaimAuditSummary(
        claim_id="C1",
        requirement_id="R1",
        target_summary="未通过的结论",
        evidence_count=1,
        fact_validation_status="mismatch",
    )
    observed = AnalysisOutcome(
        answer="已执行观察",
        evidence_refs=["audit_1"],
        claim_audits=[failed_claim],
    )

    assert _answer_data_freshness(observed) == "current_run_observation_only"


def test_answer_data_freshness_for_schema_only_clarification_is_current_schema() -> None:
    clarification = AnalysisOutcome(
        protocol_id="data-analysis",
        answer="请补充范围",
        completion_kind="clarification",
        clarification={"question": "请补充范围", "missing_items": ["data_scope"]},
    )

    assert _answer_data_freshness(clarification) == "current_schema"


def test_answer_data_freshness_for_observed_clarification_is_observation_only() -> None:
    clarification = AnalysisOutcome(
        protocol_id="data-analysis",
        answer="请补充范围",
        completion_kind="clarification",
        clarification={"question": "请补充范围", "missing_items": ["data_scope"]},
        observations_performed=True,
    )

    assert _answer_data_freshness(clarification) == "current_run_observation_only"


def test_historical_topic_prefers_claim_summary_over_partial_stub() -> None:
    stub = "当前尚未提交可验证结论，分析暂未完成。"
    summary = "确认订单与客户的外键关系"

    assert _historical_topic(summary, stub) == summary
    assert _historical_topic(None, stub) == stub
    assert len(_historical_topic("x" * 400, stub)) == 300


@pytest.mark.asyncio
async def test_finalizer_writes_markdown_and_derived_evidence_once(migrated_settings) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        finalizer = RunFinalizer(factory, RunEventNotifier())
        answer_text = "截至 2023-01-01，手机号 13800138000，邮箱 buyer@example.com。"
        outcome = AnalysisOutcome(
            answer=answer_text,
            evidence_refs=["audit_1", "table_1"],
        )

        assert await finalizer.complete("run_runtime", outcome) is True
        assert await finalizer.complete("run_runtime", outcome) is False

        async with factory() as db:
            run = await db.get(RunModel, "run_runtime")
            assert run is not None
            assert run.status == RunStatus.SUCCEEDED.value
            assert run.completion_kind == "completed"
            assert run.incomplete_reason is None
            messages = list(
                await db.scalars(select(MessageModel).where(MessageModel.run_id == run.id))
            )
            assert [message.content_text for message in messages] == [answer_text]
            assert messages[0].answer_evidence_refs_json == ["audit_1", "table_1"]
            assert messages[0].answer_sections_json is None
            events = list(
                await db.scalars(
                    select(RunEventModel)
                    .where(RunEventModel.run_id == run.id)
                    .order_by(RunEventModel.seq)
                )
            )
            assert [event.event_type for event in events] == ["answer.ready", "run.succeeded"]
            assert [event.seq for event in events] == [1, 2]
            assert events[0].payload_json["completion_kind"] == "completed"
            assert events[0].payload_json["answer_format"] == "markdown"
            assert events[0].payload_json["evidence_count"] == 2
            assert events[0].payload_json["claim_audit_summary_json"] == "[]"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_persists_clarification_as_distinct_success_kind(migrated_settings) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        finalizer = RunFinalizer(factory, RunEventNotifier())
        outcome = AnalysisOutcome(
            answer="请指定要查看的表、指标或维度。",
            completion_kind="clarification",
            clarification=AnalysisClarificationDraft(
                question="请指定要查看的表、指标或维度。",
                missing_items=["data_scope"],
            ),
        )

        assert await finalizer.complete("run_runtime", outcome) is True

        async with factory() as db:
            run = await db.get(RunModel, "run_runtime")
            assert run is not None
            assert run.status == RunStatus.SUCCEEDED.value
            assert run.completion_kind == "clarification"
            assert run.incomplete_reason is None
            event = await db.scalar(
                select(RunEventModel).where(RunEventModel.event_type == "answer.ready")
            )
            assert event is not None
            assert event.payload_json["completion_kind"] == "clarification"
            assert event.payload_json["evidence_count"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_consumes_pending_only_at_the_same_context_revision(
    migrated_settings,
) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            db.add(
                DatasourceConversationStateModel(
                    session_id="session_runtime",
                    datasource_id="datasource_runtime",
                    revision=1,
                    pending_id="pending_old",
                    pending_json={
                        "question": "请补充范围",
                        "missing_items": ["data_scope"],
                    },
                )
            )
            run = await db.get(RunModel, "run_runtime")
            assert run is not None
            run.datasource_context_revision = 1
            await db.commit()

        finalizer = RunFinalizer(factory, RunEventNotifier())
        clarification = AnalysisOutcome(
            answer="请补充时间范围。",
            completion_kind="clarification",
            clarification=AnalysisClarificationDraft(
                question="请补充时间范围。",
                missing_items=["time_range"],
            ),
        )
        assert await finalizer.complete("run_runtime", clarification) is True

        async with factory() as db:
            state = await db.get(
                DatasourceConversationStateModel,
                ("session_runtime", "datasource_runtime"),
            )
            assert state is not None
            assert state.revision == 2
            assert state.pending_id != "pending_old"
            assert state.pending_json == {
                "question": "请补充时间范围。",
                "missing_items": ["time_range"],
                "scope_summary": None,
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_completed_analysis_clears_matching_pending_revision(
    migrated_settings,
) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            db.add(
                DatasourceConversationStateModel(
                    session_id="session_runtime",
                    datasource_id="datasource_runtime",
                    revision=1,
                    pending_id="pending_old",
                    pending_json={
                        "question": "请补充范围",
                        "missing_items": ["data_scope"],
                    },
                )
            )
            run = await db.get(RunModel, "run_runtime")
            assert run is not None
            run.datasource_context_revision = 1
            await db.commit()

        finalizer = RunFinalizer(factory, RunEventNotifier())
        assert (
            await finalizer.complete(
                "run_runtime",
                AnalysisOutcome(
                    answer="本次分析已完成。",
                    protocol_id="data-analysis",
                    consumes_pending=True,
                    completion_kind="completed",
                ),
            )
            is True
        )

        async with factory() as db:
            state = await db.get(
                DatasourceConversationStateModel,
                ("session_runtime", "datasource_runtime"),
            )
            assert state is not None
            assert state.revision == 2
            assert state.pending_id is None
            assert state.pending_json is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_does_not_overwrite_newer_pending_revision(
    migrated_settings,
) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            db.add(
                DatasourceConversationStateModel(
                    session_id="session_runtime",
                    datasource_id="datasource_runtime",
                    revision=2,
                    pending_id="pending_new",
                    pending_json={
                        "question": "新问题",
                        "missing_items": ["time_range"],
                    },
                )
            )
            run = await db.get(RunModel, "run_runtime")
            assert run is not None
            run.datasource_context_revision = 1
            await db.commit()

        finalizer = RunFinalizer(factory, RunEventNotifier())
        assert (
            await finalizer.complete(
                "run_runtime",
                AnalysisOutcome(
                    answer="请补充范围。",
                    completion_kind="clarification",
                    clarification=AnalysisClarificationDraft(
                        question="旧 Run 的澄清",
                        missing_items=["data_scope"],
                    ),
                ),
            )
            is True
        )

        async with factory() as db:
            state = await db.get(
                DatasourceConversationStateModel,
                ("session_runtime", "datasource_runtime"),
            )
            assert state is not None
            assert state.revision == 2
            assert state.pending_id == "pending_new"
            assert state.pending_json["question"] == "新问题"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_persists_bounded_claim_audit_summary_on_answer_ready(
    migrated_settings,
) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        finalizer = RunFinalizer(factory, RunEventNotifier())
        outcome = AnalysisOutcome(
            answer="总额为 42。",
            claim_audits=[
                AnalysisClaimAuditSummary(
                    claim_id="C1",
                    requirement_id="R1",
                    target_summary="确认总额",
                    evidence_count=1,
                    facts=[
                        AnalysisClaimAuditFact(
                            fact_key="total",
                            name="total",
                            value=42,
                        )
                    ],
                )
            ],
        )

        assert await finalizer.complete("run_runtime", outcome) is True

        async with factory() as db:
            event = await db.scalar(
                select(RunEventModel).where(RunEventModel.event_type == "answer.ready")
            )
            assert event is not None
            assert event.payload_json["claim_audit_summary_json"] == (
                '[{"claim_id":"C1","requirement_id":"R1","target_summary":"确认总额",'
                '"evidence_count":1,"facts":[{"fact_key":"total","name":"total",'
                '"value":42,"unit":null,"dimensions":{}}],"commit_status":"passed",'
                '"fact_validation_status":"passed"}]'
            )
            assert event.payload_json["claim_audit_truncated"] is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_truncates_large_internal_claim_audit_for_answer_event(
    migrated_settings,
) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        finalizer = RunFinalizer(factory, RunEventNotifier())
        facts = [
            AnalysisClaimAuditFact(
                fact_key=f"f{index}",
                name="v",
                value=index,
            )
            for index in range(72)
        ]
        outcome = AnalysisOutcome(
            answer="分组事实已核验。",
            claim_audits=[
                AnalysisClaimAuditSummary(
                    claim_id="C1",
                    requirement_id="R1",
                    target_summary="确认分组事实",
                    evidence_count=1,
                    facts=facts,
                )
            ],
        )

        assert await finalizer.complete("run_runtime", outcome) is True

        async with factory() as db:
            event = await db.scalar(
                select(RunEventModel).where(RunEventModel.event_type == "answer.ready")
            )
            assert event is not None
            projected = json.loads(event.payload_json["claim_audit_summary_json"])
            assert len(projected[0]["facts"]) == 64
            assert event.payload_json["claim_audit_truncated"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_preserves_partial_candidate_and_marks_run_succeeded(
    migrated_settings,
) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        finalizer = RunFinalizer(factory, RunEventNotifier())
        outcome = AnalysisOutcome(
            answer="候选回答",
            completion_kind="partial",
            incomplete_reason="FINAL_ANSWER_INVALID",
        )

        assert await finalizer.complete("run_runtime", outcome) is True

        async with factory() as db:
            run = await db.get(RunModel, "run_runtime")
            assert run is not None
            assert run.status == RunStatus.SUCCEEDED.value
            assert run.completion_kind == "partial"
            assert run.incomplete_reason == "FINAL_ANSWER_INVALID"
            message = await db.get(MessageModel, run.assistant_message_id)
            assert message is not None
            assert message.content_text == "候选回答"
            assert message.answer_evidence_refs_json is None
            assert message.answer_sections_json is None
            event = await db.scalar(
                select(RunEventModel).where(RunEventModel.event_type == "answer.ready")
            )
            assert event is not None
            assert event.payload_json["completion_kind"] == "partial"
            assert event.payload_json["incomplete_reason"] == "FINAL_ANSWER_INVALID"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_global_run_timeout_fails_instead_of_succeeding_without_outcome(
    migrated_settings,
) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    executor = RunExecutor(
        session_factory=factory,
        notifier=RunEventNotifier(),
        registry=RunCancelRegistry(),
        runtime_factory=lambda _db, _events: _TimeoutRuntime(),
    )
    try:
        await executor.run(_execution_context(timeout_seconds=1))

        async with factory() as db:
            run = await db.get(RunModel, "run_runtime")
            assert run is not None
            assert run.status == RunStatus.FAILED.value
            assert run.completion_kind is None
            assert run.incomplete_reason is None
            assert run.error_code == "RUN_TIMEOUT"
            message = await db.get(MessageModel, run.assistant_message_id)
            assert message is not None
            assert message.answer_sections_json is None
            events = list(
                await db.scalars(
                    select(RunEventModel)
                    .where(RunEventModel.run_id == run.id)
                    .order_by(RunEventModel.seq)
                )
            )
            assert [event.event_type for event in events] == [
                "run.started",
                "run.failed",
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_event_pipeline_projects_tool_call_without_step_id(migrated_settings) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            pipeline = RunEventPipeline(db)
            async with pipeline.transaction("run_runtime"):
                called = RunEventCreate(
                    run_id="run_runtime",
                    type=RunEventType.TOOL_CALLED,
                    payload={
                        "tool_call_id": "tool_runtime",
                        "tool_name": "run_sql_readonly",
                        "turn_no": 1,
                    },
                    tool_input={
                        "sql": "SELECT * FROM sales",
                        "runtime": {"host_path": "must-not-persist"},
                    },
                )
                assert "tool_input" not in called.model_dump()
                assert "SELECT * FROM sales" not in repr(called)
                await pipeline.append(called)
                await pipeline.append(
                    RunEventCreate(
                        run_id="run_runtime",
                        type=RunEventType.TOOL_SUCCEEDED,
                        payload={
                            "tool_call_id": "tool_runtime",
                            "tool_name": "run_sql_readonly",
                            "turn_no": 1,
                            "elapsed_ms": 2,
                            "evidence_count": 2,
                            "output_summary_json": (
                                '{"row_count":1,"node_names":"orders、customers"}'
                            ),
                        },
                    )
                )
                await db.commit()

            tool = await db.get(ToolCallModel, "tool_runtime")
            assert tool is not None
            assert tool.tool_name == "run_sql_readonly"
            assert tool.output_summary_json == {
                "row_count": 1,
                "node_names": "orders、customers",
            }
            assert tool.input_json == {"sql": "SELECT * FROM sales"}
            assert not hasattr(tool, "step_id")
            called_event = await db.scalar(
                select(RunEventModel).where(RunEventModel.event_type == "tool.called")
            )
            assert called_event is not None
            assert called_event.payload_json == {
                "tool_call_id": "tool_runtime",
                "tool_name": "run_sql_readonly",
                "turn_no": 1,
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_event_pipeline_scopes_reused_model_tool_call_ids_by_run(migrated_settings) -> None:
    """不同 Run 可以复用模型编号，但每次事件仍更新自己的工具投影。"""

    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            seeded = await db.get(RunModel, "run_runtime")
            assert seeded is not None
            seeded.status = RunStatus.SUCCEEDED.value
            db.add(
                RunModel(
                    id="run_runtime_2",
                    session_id="session_runtime",
                    question="问题 2",
                    status=RunStatus.QUEUED.value,
                )
            )
            await db.commit()
            for run_id in ("run_runtime", "run_runtime_2"):
                pipeline = RunEventPipeline(db)
                async with pipeline.transaction(run_id):
                    await pipeline.append(
                        RunEventCreate(
                            run_id=run_id,
                            type=RunEventType.TOOL_CALLED,
                            payload={
                                "tool_call_id": "call_0",
                                "tool_name": "run_sql_readonly",
                                "turn_no": 1,
                            },
                        )
                    )
                    await pipeline.append(
                        RunEventCreate(
                            run_id=run_id,
                            type=RunEventType.TOOL_SUCCEEDED,
                            payload={
                                "tool_call_id": "call_0",
                                "tool_name": "run_sql_readonly",
                                "turn_no": 1,
                                "elapsed_ms": 1,
                                "evidence_count": 0,
                                "output_summary_json": "{}",
                            },
                        )
                    )
                    await db.commit()

            tools = list(
                await db.scalars(
                    select(ToolCallModel)
                    .where(ToolCallModel.tool_call_id == "call_0")
                    .order_by(ToolCallModel.run_id)
                )
            )
            assert len(tools) == 2
            assert {tool.run_id for tool in tools} == {"run_runtime", "run_runtime_2"}
            assert next(tool.id for tool in tools if tool.run_id == "run_runtime") == "call_0"
            assert next(tool.id for tool in tools if tool.run_id == "run_runtime_2") != "call_0"
            assert all(tool.status == "succeeded" for tool in tools)
    finally:
        await engine.dispose()


def test_answer_delta_accepts_unicode_markdown_and_normal_line_breaks() -> None:
    _validate_payload(
        RunEventType.ANSWER_DELTA,
        {
            "delta": (
                "## 结论\n\n| 月份 | 销售额 |\n| --- | ---: |\n"
                "| 1 月 | **42** |\n\n路径：`outputs/report.md`"
            ),
        },
    )


def test_answer_delta_rejects_control_characters_and_empty_text() -> None:
    with pytest.raises(ValueError, match="控制字符"):
        _validate_payload(RunEventType.ANSWER_DELTA, {"delta": "结果\x00异常"})

    with pytest.raises(ValueError, match="为空"):
        _validate_payload(RunEventType.ANSWER_DELTA, {"delta": ""})

    with pytest.raises(ValueError, match="长度限制"):
        _validate_payload(RunEventType.ANSWER_DELTA, {"delta": "a" * 4_001})


def test_answer_delta_rejects_payload_over_total_size_limit() -> None:
    with pytest.raises(ValueError, match="安全大小限制"):
        _validate_payload(RunEventType.ANSWER_DELTA, {"delta": "中" * 2_800})


def test_internal_event_identifiers_remain_strict() -> None:
    with pytest.raises(ValueError, match="有限安全值"):
        _validate_payload(
            RunEventType.TOOL_CALLED,
            {"tool_call_id": "tool_1", "tool_name": "查询工具", "turn_no": 1},
        )

    with pytest.raises(ValueError, match="有限安全值"):
        _validate_payload(
            RunEventType.RUN_FAILED,
            {"error_code": "模型失败\n泄漏", "error_message": "失败"},
        )


def test_discovery_observation_event_accepts_only_safe_scalar_summary() -> None:
    payload = {
        "tool_call_id": "discovery_call",
        "tool_name": "run_sql_readonly",
        "turn_no": 1,
        "audit_log_id": "audit_discovery",
        "artifact_id": "artifact_discovery",
        "column_count": 3,
        "row_count": 20,
        "rows_truncated": True,
    }
    _validate_payload(RunEventType.ANALYSIS_DISCOVERY_OBSERVED, payload)

    with pytest.raises(ValueError, match="未允许字段"):
        _validate_payload(
            RunEventType.ANALYSIS_DISCOVERY_OBSERVED,
            {**payload, "sql": "SELECT * FROM source"},
        )
    with pytest.raises(ValueError, match="未允许字段"):
        _validate_payload(
            RunEventType.ANALYSIS_DISCOVERY_OBSERVED,
            {**payload, "rows": [["must-not-persist"]]},
        )
    with pytest.raises(ValueError, match="有限安全值|引用无效"):
        _validate_payload(
            RunEventType.ANALYSIS_DISCOVERY_OBSERVED,
            {**payload, "artifact_id": ""},
        )


def test_preparation_events_accept_only_fixed_phases_and_safe_elapsed_time() -> None:
    _validate_payload(
        RunEventType.RUN_PREPARATION_STARTED,
        {"phase": "semantic_context"},
    )
    _validate_payload(
        RunEventType.RUN_PREPARATION_COMPLETED,
        {"phase": "analysis_plan", "elapsed_ms": 42, "status": "completed"},
    )
    _validate_payload(
        RunEventType.RUN_PREPARATION_COMPLETED,
        {
            "phase": "run_opening",
            "elapsed_ms": 42,
            "status": "timed_out",
            "failure_code": "RUN_OPENING_TIMEOUT",
            "opening_model_calls": 2,
            "opening_repair_calls": 1,
            "validation_issue_count": 1,
            "validation_issue_types": "analysis_plan_semantic_invalid",
            "validation_issue_paths": "requirements[0].fulfillment.assertions",
            "validation_tool_call_count": 0,
            "validation_invalid_tool_call_count": 1,
        },
    )

    with pytest.raises(ValueError, match="准备阶段"):
        _validate_payload(
            RunEventType.RUN_PREPARATION_STARTED,
            {"phase": "prompt:must-not-persist"},
        )

    with pytest.raises(ValueError, match="未允许字段"):
        _validate_payload(
            RunEventType.RUN_PREPARATION_COMPLETED,
            {"phase": "run_opening", "input": "must-not-persist"},
        )

    with pytest.raises(ValueError, match="状态"):
        _validate_payload(
            RunEventType.RUN_PREPARATION_COMPLETED,
            {"phase": "run_opening", "elapsed_ms": 42, "status": "running"},
        )

    with pytest.raises(ValueError, match="必须成对出现"):
        _validate_payload(
            RunEventType.RUN_PREPARATION_COMPLETED,
            {
                "phase": "run_opening",
                "elapsed_ms": 42,
                "status": "completed",
                "opening_model_calls": 1,
            },
        )

    with pytest.raises(ValueError, match="超过总调用次数"):
        _validate_payload(
            RunEventType.RUN_PREPARATION_COMPLETED,
            {
                "phase": "run_opening",
                "elapsed_ms": 42,
                "status": "completed",
                "opening_model_calls": 1,
                "opening_repair_calls": 1,
            },
        )

    with pytest.raises(ValueError, match="诊断索引"):
        _validate_payload(
            RunEventType.RUN_PREPARATION_COMPLETED,
            {
                "phase": "run_opening",
                "elapsed_ms": 42,
                "status": "failed",
                "validation_issue_paths": "requirements[0] unsafe value",
            },
        )


def test_agent_turn_events_accept_bounded_action_summary() -> None:
    _validate_payload(
        RunEventType.AGENT_TURN_STARTED,
        {"turn_no": 1},
    )
    _validate_payload(
        RunEventType.AGENT_TURN_COMPLETED,
        {
            "turn_no": 1,
            "elapsed_ms": 1840,
            "status": "completed",
            "action_kind": "tool_call",
            "tool_names": "run_sql_readonly,run_python",
            "tool_call_count": 2,
        },
    )
    _validate_payload(
        RunEventType.AGENT_TURN_COMPLETED,
        {
            "turn_no": 2,
            "elapsed_ms": 20,
            "status": "failed",
            "action_kind": "model_error",
            "tool_names": "",
            "tool_call_count": 0,
            "failure_code": "MODEL_REQUEST_FAILED",
        },
    )
    _validate_payload(
        RunEventType.AGENT_TURN_COMPLETED,
        {
            "turn_no": 2,
            "elapsed_ms": 20,
            "status": "failed",
            "action_kind": "tool_call",
            "tool_names": "run_sql_readonly",
            "tool_call_count": 1,
            "failure_code": "MODEL_OUTPUT_INVALID",
            "reason_code": "DISCOVERY_RESPONSE_FORMAT_INVALID",
        },
    )
    _validate_payload(
        RunEventType.AGENT_TURN_COMPLETED,
        {
            "turn_no": 3,
            "elapsed_ms": 20,
            "status": "completed",
            "action_kind": "respond",
            "tool_names": "",
            "tool_call_count": 0,
            "context_retry_count": 1,
            "context_compaction_count": 2,
            "working_set_count": 3,
            "working_set_compacted": True,
            "model_input_chars": 12_000,
            "model_input_tokens": 3_000,
            "estimated_total_tokens": 4_000,
            "context_window_tokens": 8_192,
            "input_budget_tokens": 7_000,
            "remaining_tokens": 4_192,
            "estimate_source": "utf8_estimate",
            "working_set_value_count": 4,
        },
    )

    with pytest.raises(ValueError, match="工具摘要"):
        _validate_payload(
            RunEventType.AGENT_TURN_COMPLETED,
            {
                "turn_no": 1,
                "elapsed_ms": 20,
                "status": "completed",
                "action_kind": "tool_call",
                "tool_names": "SELECT *",
                "tool_call_count": 1,
            },
        )


def test_final_answer_observability_events_only_accept_safe_timing_fields() -> None:
    _validate_payload(
        RunEventType.FINAL_ANSWER_REQUEST_STARTED,
        {"attempt": 1, "mode": "json_schema"},
    )
    _validate_payload(
        RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED,
        {"attempt": 1, "mode": "markdown", "elapsed_ms": 42},
    )
    _validate_payload(
        RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
        {
            "attempt": 1,
            "mode": "submit_answer",
            "elapsed_ms": 42,
            "failure_code": "MODEL_OUTPUT_INVALID",
            "validation_stage": "dto",
        },
    )
    _validate_payload(
        RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT,
        {
            "attempt": 2,
            "mode": "json_schema",
            "elapsed_ms": 45_000,
            "failure_code": "FINAL_ANSWER_TIMEOUT",
        },
    )
    _validate_payload(
        RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
        {
            "attempt": 1,
            "mode": "markdown",
            "elapsed_ms": 42,
            "failure_code": "FINAL_ANSWER_FACT_MISMATCH",
            "validation_stage": "markdown",
        },
    )
    _validate_payload(
        RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
        {
            "attempt": 1,
            "mode": "markdown",
            "elapsed_ms": 42,
            "failure_code": "CONTEXT_BUDGET_EXHAUSTED",
            "validation_stage": "budget",
        },
    )

    _validate_payload(
        RunEventType.ANSWER_READY,
        {
            "assistant_message_id": "message_1",
            "evidence_count": 1,
            "artifact_count": 0,
            "answer_format": "markdown",
            "completion_kind": "completed",
            "claim_audit_truncated": False,
            "answer_data_freshness": "current_run_evidence",
            "historical_context_injected": False,
            "historical_summary_count": 0,
            "claim_audit_summary_json": (
                '[{"claim_id":"C1","requirement_id":"R1",'
                '"target_summary":"总额","evidence_count":1,'
                '"facts":[{"fact_key":"total","name":"total",'
                '"value":42,"unit":null,"dimensions":{}}]}]'
            ),
        },
    )

    with pytest.raises(ValueError, match="尝试序号"):
        _validate_payload(
            RunEventType.FINAL_ANSWER_REQUEST_STARTED,
            {"attempt": 0, "mode": "json_schema"},
        )

    with pytest.raises(ValueError, match="输出模式"):
        _validate_payload(
            RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED,
            {"attempt": 1, "mode": "free_text", "elapsed_ms": 42},
        )

    with pytest.raises(ValueError, match="失败分类"):
        _validate_payload(
            RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
            {
                "attempt": 1,
                "mode": "json_schema",
                "elapsed_ms": 42,
                "failure_code": "MODEL_REQUEST_FAILED",
                "validation_stage": "dto",
            },
        )

    with pytest.raises(ValueError, match="未允许字段"):
        _validate_payload(
            RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
            {
                "attempt": 1,
                "mode": "json_schema",
                "elapsed_ms": 42,
                "failure_code": "MODEL_OUTPUT_INVALID",
                "markdown": "不得持久化",
            },
        )


@pytest.mark.asyncio
async def test_final_answer_observability_events_persist_through_run_event_pipeline(
    migrated_settings,
) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            pipeline = RunEventPipeline(db)
            async with pipeline.transaction("run_runtime"):
                await pipeline.append(
                    RunEventCreate(
                        run_id="run_runtime",
                        type=RunEventType.FINAL_ANSWER_REQUEST_STARTED,
                        payload={"attempt": 1, "mode": "json_schema"},
                    )
                )
                await pipeline.append(
                    RunEventCreate(
                        run_id="run_runtime",
                        type=RunEventType.FINAL_ANSWER_RESPONSE_RECEIVED,
                        payload={"attempt": 1, "mode": "json_schema", "elapsed_ms": 42},
                    )
                )
                await pipeline.append(
                    RunEventCreate(
                        run_id="run_runtime",
                        type=RunEventType.FINAL_ANSWER_VALIDATION_FAILED,
                        payload={
                            "attempt": 1,
                            "mode": "json_schema",
                            "elapsed_ms": 42,
                            "failure_code": "MODEL_OUTPUT_INVALID",
                            "validation_stage": "dto",
                        },
                    )
                )
                await pipeline.append(
                    RunEventCreate(
                        run_id="run_runtime",
                        type=RunEventType.FINAL_ANSWER_REQUEST_TIMED_OUT,
                        payload={
                            "attempt": 2,
                            "mode": "markdown",
                            "elapsed_ms": 45_000,
                            "failure_code": "FINAL_ANSWER_TIMEOUT",
                        },
                    )
                )
                await db.commit()

            events = list(
                await db.scalars(
                    select(RunEventModel)
                    .where(RunEventModel.run_id == "run_runtime")
                    .order_by(RunEventModel.seq)
                )
            )
            assert [(event.seq, event.event_type) for event in events] == [
                (1, "final_answer.request.started"),
                (2, "final_answer.response.received"),
                (3, "final_answer.validation.failed"),
                (4, "final_answer.request.timed_out"),
            ]
            assert events[-1].payload_json == {
                "attempt": 2,
                "mode": "markdown",
                "elapsed_ms": 45_000,
                "failure_code": "FINAL_ANSWER_TIMEOUT",
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tool_failure_projects_safe_guard_message_to_tool_call(migrated_settings) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            pipeline = RunEventPipeline(db)
            async with pipeline.transaction("run_runtime"):
                await pipeline.append(
                    RunEventCreate(
                        run_id="run_runtime",
                        type=RunEventType.TOOL_CALLED,
                        payload={
                            "tool_call_id": "tool_guard",
                            "tool_name": "run_sql_readonly",
                            "turn_no": 1,
                        },
                        tool_input={"sql": "SELECT AVG(not.fully.paid) FROM dataset"},
                    )
                )
                await pipeline.append(
                    RunEventCreate(
                        run_id="run_runtime",
                        type=RunEventType.TOOL_FAILED,
                        payload={
                            "tool_call_id": "tool_guard",
                            "tool_name": "run_sql_readonly",
                            "turn_no": 1,
                            "elapsed_ms": 20,
                            "output_summary_json": "{}",
                            "error_code": "DATA_GATEWAY_BLOCKED",
                            "reason_code": "SQL_PARSE_ERROR",
                            "error_message": "SQL 无法解析",
                            "hint": "请将带点号字段逐字包为双引号字段名。",
                            "retryable": True,
                            "subject": "AVG(not.fully.paid)",
                            "line": 1,
                            "column": 15,
                        },
                    )
                )
                await db.commit()

            tool = await db.get(ToolCallModel, "tool_guard")
            assert tool is not None
            assert tool.status == "failed"
            assert tool.error_code == "DATA_GATEWAY_BLOCKED"
            assert tool.error_message == "SQL 无法解析"
    finally:
        await engine.dispose()


def test_tool_failure_accepts_only_safe_guard_fields() -> None:
    _validate_payload(
        RunEventType.TOOL_FAILED,
        {
            "tool_call_id": "tool_guard",
            "tool_name": "run_sql_readonly",
            "turn_no": 1,
            "elapsed_ms": 20,
            "output_summary_json": "{}",
            "error_code": "DATA_GATEWAY_BLOCKED",
            "reason_code": "SQL_PARSE_ERROR",
            "error_message": "SQL 无法解析",
            "hint": "请改为一条只读 SQL。",
            "retryable": True,
            "subject": "AVG(not.fully.paid)",
            "line": 1,
            "column": 15,
        },
    )


@pytest.mark.asyncio
async def test_sql_audit_repository_links_rewrite_to_same_run_and_schema(migrated_settings) -> None:
    await _seed_run(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            audits = SqlAuditRepository(db)
            initial = await audits.create_proposed(
                datasource_id="datasource_runtime",
                schema_revision=1,
                run_id="run_runtime",
                tool_call_id="tool_initial",
                original_sql="SELECT missing FROM dataset",
            )
            rewrite = await audits.create_proposed(
                datasource_id="datasource_runtime",
                schema_revision=1,
                run_id="run_runtime",
                tool_call_id="tool_rewrite",
                repaired_from_id=initial.id,
                original_sql="SELECT value FROM dataset",
            )
            await db.commit()

            assert initial.attempt_no == 0
            assert rewrite.attempt_no == 1
            assert rewrite.repaired_from_id == initial.id
    finally:
        await engine.dispose()
