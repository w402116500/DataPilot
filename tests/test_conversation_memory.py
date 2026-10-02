from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agent_runtime.contracts import AnalysisOutcome
from contracts.status import DataSourceStatus, MessageRole, RunStatus
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import (
    DatasourceConversationStateModel,
    DataSourceModel,
    MessageModel,
    ModelProfileModel,
    RunModel,
    SessionModel,
)
from metadata.repositories import (
    HistoricalAnswerSummaryRepository,
    MessageRepository,
    RunRepository,
)
from runtime.conversation_memory import ConversationMemoryService, _safe_memory_text
from runtime.run_event_pipeline import RunEventNotifier
from runtime.run_finalizer import RunFinalizer
from sqlalchemy import select


def test_memory_filter_preserves_dotted_field_names_but_rejects_sql() -> None:
    """SQL 关键词出现在字段名中时仍应保留普通会话文本。"""

    field_text = "请分析字段 days.with.cr.line 和 path.with.name"

    assert _safe_memory_text(field_text, maximum=900) == field_text
    assert _safe_memory_text("SELECT days.with.cr.line FROM loans", maximum=900) is None
    assert _safe_memory_text("密码 secret-value", maximum=900) is None
    assert _safe_memory_text("文件位于 C:\\private\\loans.csv", maximum=900) is None


@pytest.mark.parametrize(
    "text",
    [
        "预测/分类；训练/测试集",
        "同比/环比，以及训练/测试集的划分方式",
    ],
)
def test_memory_filter_allows_slash_prose(text: str) -> None:
    """普通概念表达中的斜杠不能被当成绝对路径。"""

    assert _safe_memory_text(text, maximum=900) == text


@pytest.mark.parametrize(
    "text",
    [
        "文件位于 C:\\private\\loans.csv",
        "文件位于 C:/private/loans.csv",
        "文件位于 \\\\server\\share\\loans.csv",
        *[
            f"文件位于 /{root}/report.csv"
            for root in ("Users", "home", "tmp", "var", "etc", "workspace")
        ],
    ],
)
def test_memory_filter_rejects_high_confidence_absolute_paths(text: str) -> None:
    """真实 Windows、UNC 和受控 POSIX 路径仍不能进入会话记忆。"""

    assert _safe_memory_text(text, maximum=900) is None


async def _seed_context_dependencies(settings) -> None:
    """创建独立会话和 Run 所需对象，供会话上下文测试使用。"""

    engine = create_sqlite_engine(settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            db.add_all(
                [
                    DataSourceModel(
                        id="datasource_a",
                        name="A",
                        description=None,
                        type="csv",
                        source_ref="datasource_a/source.csv",
                        file_size=1,
                        content_hash="hash_a",
                        schema_cache_json={"datasource_id": "datasource_a", "tables": []},
                        schema_revision=1,
                        status=DataSourceStatus.READY.value,
                        datalink_graph_version="graph_a",
                    ),
                    ModelProfileModel(
                        id="profile_1",
                        name="Demo model",
                        provider="openai-compatible",
                        model_name="demo-model",
                        base_url="https://model.example/v1",
                        temperature=0,
                        run_timeout_seconds=60,
                        status="tested",
                        is_active=True,
                    ),
                    SessionModel(id="session_1", title="当前会话"),
                    SessionModel(id="session_2", title="另一个会话"),
                ]
            )
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_only_contains_current_session_messages_and_history(
    migrated_settings,
) -> None:
    """新会话不会带入另一会话的提问、结论或摘要。"""

    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            messages = MessageRepository(db)
            await messages.create(
                session_id="session_1",
                role=MessageRole.USER,
                content_text="当前会话的问题",
                datasource_id="datasource_a",
            )
            await messages.create(
                session_id="session_1",
                role=MessageRole.ASSISTANT,
                content_text="当前会话的结论",
                datasource_id="datasource_a",
            )
            await messages.create(
                session_id="session_2",
                role=MessageRole.USER,
                content_text="另一个会话的问题",
            )
            await messages.create(
                session_id="session_2",
                role=MessageRole.ASSISTANT,
                content_text="另一个会话的订单数是 8015",
                datasource_id="datasource_a",
            )
            await db.commit()

        async with session_factory() as db:
            context = await ConversationMemoryService(db).build_context(
                session_id="session_1",
                datasource_id="datasource_a",
            )
            visible_text = "\n".join(item.content_text for item in context.recent_user_turns)

            assert "当前会话的问题" in visible_text
            assert "当前会话的结论" not in visible_text
            assert "另一个会话" not in visible_text
            assert context.historical_summaries == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_excludes_messages_from_switched_datasource(
    migrated_settings,
) -> None:
    """同一 Session 换源后，旧数据源的问答和摘要不能进入新 Run。"""

    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            messages = MessageRepository(db)
            await messages.create(
                session_id="session_1",
                role=MessageRole.USER,
                content_text="数据源 A 的问题",
                datasource_id="datasource_a",
            )
            await messages.create(
                session_id="session_1",
                role=MessageRole.ASSISTANT,
                content_text="数据源 A 的总记录数是 111",
                datasource_id="datasource_a",
            )
            await db.commit()

        async with session_factory() as db:
            context = await ConversationMemoryService(db).build_context(
                session_id="session_1",
                datasource_id="datasource_b",
            )
            assert context.recent_user_turns == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_excludes_legacy_messages_without_datasource_scope(migrated_settings) -> None:
    """没有来源范围的旧消息可保留在历史中，但不能进入新分析 Prompt。"""

    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            await MessageRepository(db).create(
                session_id="session_1",
                role=MessageRole.ASSISTANT,
                content_text="旧数据源结论 999",
            )
            await db.commit()

        async with session_factory() as db:
            context = await ConversationMemoryService(db).build_context(
                session_id="session_1",
                datasource_id="datasource_a",
            )
            assert context.recent_user_turns == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_recent_projection_bounds_user_turns_and_excludes_assistant_text(
    migrated_settings,
) -> None:
    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            messages = MessageRepository(db)
            for index in range(9):
                await messages.create(
                    session_id="session_1",
                    role=MessageRole.USER,
                    # Keep six bounded turns under the aggregate 4,000-char
                    # budget while still exercising the per-message limit.
                    content_text=f"用户问题 {index} " + ("x" * 580),
                    datasource_id="datasource_a",
                )
                await messages.create(
                    session_id="session_1",
                    role=MessageRole.ASSISTANT,
                    content_text="旧回答包含 SQL SELECT secret FROM private_table",
                    datasource_id="datasource_a",
                )
            await db.commit()

        async with session_factory() as db:
            context = await ConversationMemoryService(db).build_context(
                session_id="session_1",
                datasource_id="datasource_a",
            )

            assert len(context.recent_user_turns) == 6
            assert all(item.role == "user" for item in context.recent_user_turns)
            assert all(len(item.content_text) <= 800 for item in context.recent_user_turns)
            assert sum(len(item.content_text) for item in context.recent_user_turns) <= 4_000
            assert "旧回答包含 SQL" not in "\n".join(
                item.content_text for item in context.recent_user_turns
            )
            assert "用户问题 8" in context.recent_user_turns[-1].content_text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_marks_malformed_pending_as_degraded_without_erasing_other_sources(
    migrated_settings,
) -> None:
    """损坏的 pending 不能被静默当成空状态，但可保留安全的近期用户原话。"""

    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            await MessageRepository(db).create(
                session_id="session_1",
                role=MessageRole.USER,
                content_text="继续看当前数据源的字段",
                datasource_id="datasource_a",
            )
            db.add(
                DatasourceConversationStateModel(
                    session_id="session_1",
                    datasource_id="datasource_a",
                    revision=3,
                    pending_id="pending_broken",
                    pending_json={"question": ""},
                )
            )
            await db.commit()

        async with session_factory() as db:
            context = await ConversationMemoryService(db).build_context(
                session_id="session_1",
                datasource_id="datasource_a",
            )

            assert context.pending_clarification is None
            assert context.load_status == "degraded"
            assert [item.content_text for item in context.recent_user_turns] == [
                "继续看当前数据源的字段"
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_snapshot_ids_match_only_safe_historical_summaries(migrated_settings) -> None:
    """快照里的摘要 ID 必须和真正可投影的安全摘要一一对应。"""

    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            db.add_all(
                [
                    RunModel(
                        id="run_history_unsafe",
                        session_id="session_1",
                        datasource_id="datasource_a",
                        question="unsafe",
                        status=RunStatus.SUCCEEDED.value,
                        finished_at=datetime.now(UTC),
                    ),
                    RunModel(
                        id="run_history_safe",
                        session_id="session_1",
                        datasource_id="datasource_a",
                        question="safe",
                        status=RunStatus.SUCCEEDED.value,
                        finished_at=datetime.now(UTC),
                    ),
                ]
            )
            await db.flush()
            repository = HistoricalAnswerSummaryRepository(db)
            await repository.create_if_absent(
                session_id="session_1",
                datasource_id="datasource_a",
                source_run_id="run_history_unsafe",
                topic="unsafe",
                content_text="SELECT secret FROM private_table",
                data_freshness="historical_not_current",
                source_run_finished_at=datetime(2026, 8, 30, tzinfo=UTC),
            )
            safe_summary = await repository.create_if_absent(
                session_id="session_1",
                datasource_id="datasource_a",
                source_run_id="run_history_safe",
                topic="safe",
                content_text="可以继续分析当前数据源",
                data_freshness="historical_not_current",
                source_run_finished_at=datetime(2026, 8, 29, tzinfo=UTC),
            )
            await db.commit()

        async with session_factory() as db:
            snapshot = await ConversationMemoryService(db).build_context_snapshot(
                session_id="session_1",
                datasource_id="datasource_a",
            )

            assert snapshot.historical_summary_ids == [safe_summary.id]
            assert len(snapshot.historical_summary_ids) == len(
                snapshot.context.historical_summaries
            )
            assert snapshot.context.historical_summaries[0].content_text == (
                "可以继续分析当前数据源"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_historical_answer_summaries_are_datasource_scoped(migrated_settings) -> None:
    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            db.add(
                DataSourceModel(
                    id="datasource_b",
                    name="B",
                    description=None,
                    type="csv",
                    source_ref="datasource_b/source.csv",
                    file_size=1,
                    content_hash="hash_b",
                    schema_cache_json={"datasource_id": "datasource_b", "tables": []},
                    schema_revision=1,
                    status=DataSourceStatus.READY.value,
                )
            )
            await db.flush()
            db.add_all(
                [
                    RunModel(
                        id="run_a",
                        session_id="session_1",
                        datasource_id="datasource_a",
                        question="A",
                        status=RunStatus.SUCCEEDED.value,
                    ),
                    RunModel(
                        id="run_b",
                        session_id="session_1",
                        datasource_id="datasource_b",
                        question="B",
                        status=RunStatus.SUCCEEDED.value,
                    ),
                ]
            )
            await db.flush()
            repository = HistoricalAnswerSummaryRepository(db)
            await repository.create_if_absent(
                session_id="session_1",
                datasource_id="datasource_a",
                source_run_id="run_a",
                topic="A 主题",
                content_text="A 数据源的历史结论",
                data_freshness="historical_not_current",
            )
            await repository.create_if_absent(
                session_id="session_1",
                datasource_id="datasource_b",
                source_run_id="run_b",
                topic="B 主题",
                content_text="B 数据源的历史结论",
                data_freshness="historical_not_current",
            )
            await db.commit()

        async with session_factory() as db:
            context = await ConversationMemoryService(db).build_context(
                session_id="session_1",
                datasource_id="datasource_a",
            )
            assert [item.content_text for item in context.historical_summaries] == [
                "A 数据源的历史结论"
            ]
            assert context.historical_summaries[0].provenance == "historical_answer_summary"
            assert context.historical_summaries[0].data_freshness == "historical_not_current"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_historical_summary_repository_rejects_cross_owner_source_run(
    migrated_settings,
) -> None:
    """仓储层自身也必须阻止摘要挂到其它 Session/DataSource 的 Run 上。"""

    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            db.add(
                RunModel(
                    id="run_owned_by_a",
                    session_id="session_1",
                    datasource_id="datasource_a",
                    question="A",
                    status=RunStatus.SUCCEEDED.value,
                    finished_at=datetime.now(UTC),
                )
            )
            await db.flush()
            with pytest.raises(ValueError, match="ownership mismatch"):
                await HistoricalAnswerSummaryRepository(db).create_if_absent(
                    session_id="session_1",
                    datasource_id="datasource_b",
                    source_run_id="run_owned_by_a",
                    topic="越权",
                    content_text="不应写入",
                    data_freshness="historical_not_current",
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_successful_finalizer_updates_only_current_session_summary(migrated_settings) -> None:
    """成功 Run 只更新本会话摘要，正式答案和终态仍正常落库。"""

    await _seed_context_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            messages = MessageRepository(db)
            for index in range(17):
                await messages.create(
                    session_id="session_1",
                    role=MessageRole.USER,
                    content_text=("以后回答请简短" if index == 0 else f"当前会话普通问题 {index}"),
                    datasource_id="datasource_a",
                )
            user = await messages.create(
                session_id="session_1",
                role=MessageRole.USER,
                content_text="当前会话的最终问题",
                datasource_id="datasource_a",
            )
            await RunRepository(db).create(
                run_id="run_finalizer",
                session_id="session_1",
                datasource_id="datasource_a",
                user_message_id=user.id,
                question=user.content_text,
                idempotency_key="run_finalizer",
                model_profile_id="profile_1",
                model_provider="openai-compatible",
                model_name="demo-model",
                schema_revision=1,
                datalink_graph_version="graph_a",
                run_timeout_seconds=60,
                input_snapshot_ref="snapshots/run_finalizer/source.csv",
                final_output_mode="json_schema",
                model_capability_fingerprint="a" * 64,
            )
            await RunRepository(db).select_protocol_if_unset("run_finalizer", "data-analysis")
            await db.commit()

        finalizer = RunFinalizer(session_factory, RunEventNotifier())
        assert await finalizer.complete(
            "run_finalizer",
            AnalysisOutcome(
                answer="已验证的结论",
                evidence_refs=["audit", "artifact"],
            ),
        )

        async with session_factory() as db:
            run = await db.get(RunModel, "run_finalizer")
            summaries = await HistoricalAnswerSummaryRepository(db).list_recent(
                session_id="session_1", datasource_id="datasource_a"
            )
            assistant_messages = list(
                await db.scalars(select(MessageModel).where(MessageModel.run_id == "run_finalizer"))
            )

            assert run is not None
            assert run.status == RunStatus.SUCCEEDED.value
            assert [message.content_text for message in assistant_messages] == ["已验证的结论"]
            # Analysis history requires a passed Claim audit; bare evidence
            # references must not be promoted to a reusable conclusion.
            assert summaries == []
    finally:
        await engine.dispose()
