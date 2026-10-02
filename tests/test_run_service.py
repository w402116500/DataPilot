from __future__ import annotations

from uuid import uuid4

import pytest
from agent_runtime.contracts import SchemaContext
from agent_runtime.graph import _AnalysisScope, _initial_messages
from application.run_execution import RunExecutionContext
from application.secret_cipher import SecretCipher
from contracts.datasources import SchemaSummaryRead, SchemaTableRead
from contracts.errors import AppError, ErrorCode
from contracts.model_profiles import CAPABILITY_CONTRACT_VERSION
from contracts.runs import RunCreate
from contracts.status import DataSourceStatus, MessageRole, RunStatus
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import (
    DataSourceModel,
    MessageModel,
    ModelProfileModel,
    RunEventModel,
    RunModel,
    SecretModel,
    SessionModel,
)
from metadata.repositories import MessageRepository
from runtime.run_context_resolver import RunContextResolver
from runtime.run_event_pipeline import RunEventPipeline
from runtime.run_service import RunService
from server.config import Settings
from sqlalchemy import select


async def _seed_ready_run_dependencies(settings: Settings) -> None:
    """写入一个 ready 数据源、激活模型和已选择数据源的会话。"""

    source_path = settings.datasource_root / "datasource_1" / "source.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("value\n1\n", encoding="utf-8")
    engine = create_sqlite_engine(settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    cipher = SecretCipher(settings.secret_master_key)
    try:
        async with session_factory() as db:
            secret = SecretModel(
                id="secret_1",
                encrypted_value=cipher.encrypt("test-api-key"),
            )
            datasource = DataSourceModel(
                id="datasource_1",
                name="Demo",
                description=None,
                type="csv",
                source_ref="datasource_1/source.csv",
                file_size=8,
                content_hash="hash",
                schema_cache_json={
                    "datasource_id": "datasource_1",
                    "dialect": "duckdb",
                    "tables": [],
                },
                schema_revision=1,
                mask_fields_confirmed=True,
                status=DataSourceStatus.READY.value,
                datalink_graph_version="graph_1",
            )
            profile = ModelProfileModel(
                id="profile_1",
                name="Demo model",
                provider="openai-compatible",
                model_name="demo-model",
                base_url="https://model.example/v1",
                temperature=0,
                run_timeout_seconds=60,
                secret_ref=secret.id,
                status="tested",
                is_active=True,
                tool_calling_supported=True,
                final_output_mode="json_schema",
                capability_contract_version=CAPABILITY_CONTRACT_VERSION,
                capability_fingerprint="a" * 64,
            )
            session = SessionModel(
                id="session_1",
                title="Demo session",
                selected_datasource_id=datasource.id,
            )
            db.add(secret)
            await db.flush()
            db.add(datasource)
            await db.flush()
            db.add_all([profile, session])
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolver_freezes_current_session_history_preferences_and_clarification(
    migrated_settings: Settings,
) -> None:
    """Resolver 将当前数据源范围内的连续对话背景放入本次 Run 上下文。"""

    await _seed_ready_run_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            messages = MessageRepository(db)
            await messages.create(
                session_id="session_1",
                role=MessageRole.USER,
                content_text="以后回答请简短，继续上次的分析",
                datasource_id="datasource_1",
            )
            await messages.create(
                session_id="session_1",
                role=MessageRole.ASSISTANT,
                content_text="请补充需要分析的范围。",
                datasource_id="datasource_1",
            )
            session = await db.get(SessionModel, "session_1")
            assert session is not None
            high_water_before_run = await messages.max_position(session.id)

            execution = await RunContextResolver(
                db,
                migrated_settings,
                SecretCipher(migrated_settings.secret_master_key),
            ).resolve(
                run_id=f"run_context_{uuid4().hex}",
                session=session,
                question="请继续",
                high_water_before_run=high_water_before_run,
            )

            context = execution.run_context.conversation_context
            assert [item.content_text for item in context.recent_user_turns] == [
                "以后回答请简短，继续上次的分析",
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_switched_datasource_memory_is_absent_from_resolved_agent_prompt(
    migrated_settings: Settings,
) -> None:
    """Resolver 传给 Graph 的 Prompt 不包含同 Session 旧数据源的事实。"""

    await _seed_ready_run_dependencies(migrated_settings)
    source_path = migrated_settings.datasource_root / "datasource_2" / "source.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("value\n2\n", encoding="utf-8")
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            db.add(
                DataSourceModel(
                    id="datasource_2",
                    name="Switched source",
                    description=None,
                    type="csv",
                    source_ref="datasource_2/source.csv",
                    file_size=8,
                    content_hash="hash_2",
                    schema_cache_json={
                        "datasource_id": "datasource_2",
                        "dialect": "duckdb",
                        "tables": [],
                    },
                    schema_revision=1,
                    mask_fields_confirmed=True,
                    status=DataSourceStatus.READY.value,
                    datalink_graph_version="graph_2",
                )
            )
            messages = MessageRepository(db)
            await messages.create(
                session_id="session_1",
                role=MessageRole.ASSISTANT,
                content_text="数据源一的总记录数是 111，未偿还数是 22",
                datasource_id="datasource_1",
            )
            session = await db.get(SessionModel, "session_1")
            assert session is not None
            session.selected_datasource_id = "datasource_2"
            high_water_before_run = await messages.max_position(session.id)
            await db.commit()

            execution = await RunContextResolver(
                db,
                migrated_settings,
                SecretCipher(migrated_settings.secret_master_key),
            ).resolve(
                run_id=f"run_switched_source_{uuid4().hex}",
                session=session,
                question="分析当前数据源",
                high_water_before_run=high_water_before_run,
            )

            schema = SchemaContext(
                datasource_id="datasource_2",
                schema_revision=1,
                schema_summary=SchemaSummaryRead(
                    datasource_id="datasource_2",
                    dialect="duckdb",
                    tables=[SchemaTableRead(name="dataset", columns=[])],
                ),
            )
            initial_prompt = _initial_messages(
                execution.run_context,
                schema,
                _AnalysisScope(),
                None,
            )[0].content

            assert execution.run_context.conversation_context.recent_user_turns == []
            assert "111" not in initial_prompt
            assert "22" not in initial_prompt
    finally:
        await engine.dispose()


async def _with_run_service(
    settings: Settings,
    callback,
) -> None:
    """在真实事务中装配 RunService，后台调度只记录私有内存上下文。"""

    engine = create_sqlite_engine(settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    scheduled: list[RunExecutionContext] = []
    notifications: list[str] = []

    async def notify(run_id: str) -> None:
        notifications.append(run_id)

    try:
        async with session_factory() as db:
            service = RunService(
                db=db,
                resolver=RunContextResolver(
                    db,
                    settings,
                    SecretCipher(settings.secret_master_key),
                ),
                events=RunEventPipeline(db, notify=notify),
                schedule=scheduled.append,
            )
            await callback(db, service, scheduled, notifications)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_run_is_idempotent_and_keeps_key_only_in_memory(
    migrated_settings: Settings,
) -> None:
    """同键同问题复用 Run；Run 表和事件中没有 API Key 或完整快照。"""

    await _seed_ready_run_dependencies(migrated_settings)

    async def exercise(db, service, scheduled, notifications) -> None:
        first = await service.create(
            "session_1",
            RunCreate(question="统计数值", idempotency_key="question_1"),
        )
        repeated = await service.create(
            "session_1",
            RunCreate(question="统计数值", idempotency_key="question_1"),
        )

        assert repeated == first
        assert len(scheduled) == 1
        assert scheduled[0].api_key == "test-api-key"
        assert scheduled[0].run_context.run_id == first.run_id
        assert notifications == [first.run_id]

        run = await db.get(RunModel, first.run_id)
        assert run is not None
        assert run.status == RunStatus.QUEUED.value
        assert run.input_snapshot_ref is not None
        assert run.model_secret_ref is None
        assert run.run_timeout_seconds == 60
        assert run.final_output_mode == "json_schema"
        assert run.model_capability_fingerprint == "a" * 64
        assert scheduled[0].model.final_output_mode == "json_schema"
        assert scheduled[0].model.model_capability_fingerprint == "a" * 64
        event = await db.scalar(select(RunEventModel).where(RunEventModel.run_id == first.run_id))
        assert event is not None
        assert event.seq == 1
        assert event.event_type == "run.queued"
        assert event.payload_json == {}
        assert "test-api-key" not in str(event.payload_json)
        message = await db.scalar(select(MessageModel).where(MessageModel.run_id == first.run_id))
        assert message is not None
        assert message.content_text == "统计数值"
        assert message.run_id == first.run_id
        assert run.user_message_id == message.id
        history = await service.get(first.run_id)
        assert history.user_message_id == message.id

        with pytest.raises(AppError) as caught:
            await service.create(
                "session_1",
                RunCreate(question="换一个问题", idempotency_key="question_1"),
            )
        assert caught.value.code is ErrorCode.IDEMPOTENCY_KEY_CONFLICT

    await _with_run_service(migrated_settings, exercise)


@pytest.mark.asyncio
async def test_create_run_requires_a_valid_model_capability_snapshot(
    migrated_settings: Settings,
) -> None:
    """仅有旧的 tested 状态不足以创建分析 Run，必须有本次探测的完整能力结果。"""

    await _seed_ready_run_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            profile = await db.get(ModelProfileModel, "profile_1")
            assert profile is not None
            profile.tool_calling_supported = None
            profile.final_output_mode = None
            profile.capability_fingerprint = None
            await db.commit()
    finally:
        await engine.dispose()

    async def exercise(db, service, scheduled, notifications) -> None:
        with pytest.raises(AppError) as caught:
            await service.create("session_1", RunCreate(question="统计数值", idempotency_key="one"))
        assert caught.value.code is ErrorCode.MODEL_PROFILE_NOT_READY
        assert scheduled == []
        assert notifications == []
        assert await db.scalar(select(RunModel.id)) is None
        assert await db.scalar(select(MessageModel.id)) is None

    await _with_run_service(migrated_settings, exercise)


@pytest.mark.asyncio
async def test_create_run_rejects_second_active_request_before_scheduling(
    migrated_settings: Settings,
) -> None:
    """会话只允许一条 queued/running Run，第二个问题不能进入后台。"""

    await _seed_ready_run_dependencies(migrated_settings)

    async def exercise(_db, service, scheduled, _notifications) -> None:
        await service.create("session_1", RunCreate(question="第一个问题", idempotency_key="one"))
        with pytest.raises(AppError) as caught:
            await service.create(
                "session_1",
                RunCreate(question="第二个问题", idempotency_key="two"),
            )
        assert caught.value.code is ErrorCode.RUN_ALREADY_ACTIVE
        assert len(scheduled) == 1

    await _with_run_service(migrated_settings, exercise)


@pytest.mark.asyncio
async def test_create_run_validates_dependencies_before_writing_message_or_run(
    migrated_settings: Settings,
) -> None:
    """没有激活模型时请求直接失败，不能留下半条消息或 queued Run。"""

    await _seed_ready_run_dependencies(migrated_settings)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            profile = await db.get(ModelProfileModel, "profile_1")
            assert profile is not None
            profile.is_active = False
            await db.commit()
    finally:
        await engine.dispose()

    async def exercise(db, service, scheduled, notifications) -> None:
        with pytest.raises(AppError) as caught:
            await service.create("session_1", RunCreate(question="统计数值", idempotency_key="one"))
        assert caught.value.code is ErrorCode.MODEL_PROFILE_NOT_READY
        assert scheduled == []
        assert notifications == []
        assert await db.scalar(select(RunModel.id)) is None
        assert await db.scalar(select(MessageModel.id)) is None

    await _with_run_service(migrated_settings, exercise)
