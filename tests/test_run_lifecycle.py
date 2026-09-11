from __future__ import annotations

import pytest
from application.artifacts import ArtifactStore
from application.datasources import DataSourceService
from application.sessions import SessionService
from contracts.datalink import DataLinkRemoveResult
from contracts.run_events import RunEventCreate, RunEventType
from contracts.status import DataSourceStatus, MessageRole, RunStatus
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import (
    ArtifactCleanupTaskModel,
    ArtifactModel,
    DataSourceModel,
    HistoricalAnswerSummaryModel,
    MessageModel,
    ModelProfileModel,
    RunEventModel,
    RunModel,
    SessionModel,
)
from metadata.repositories import (
    ArtifactRepository,
    DataSourceRepository,
    MessageRepository,
    RunRepository,
    SessionRepository,
)
from runtime.run_event_pipeline import RunEventNotifier, RunEventPipeline
from runtime.run_lifecycle import RunLifecycleCoordinator, RunRecoveryService
from server.config import Settings
from sqlalchemy import select


async def _seed_active_run(settings: Settings, *, run_id: str, datasource_id: str) -> None:
    """写入没有进程内上下文的 queued Run，模拟重启或删除时的历史状态。"""

    suffix = run_id.removeprefix("run_")
    session_id = f"session_{suffix}"
    profile_id = f"profile_{suffix}"
    engine = create_sqlite_engine(settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            db.add_all(
                [
                    DataSourceModel(
                        id=datasource_id,
                        name="Demo",
                        description=None,
                        type="csv",
                        source_ref=f"{datasource_id}/source.csv",
                        file_size=8,
                        content_hash="hash",
                        schema_cache_json={"tables": []},
                        schema_revision=1,
                        status=DataSourceStatus.READY.value,
                        datalink_graph_version="graph_1",
                    ),
                    ModelProfileModel(
                        id=profile_id,
                        name="Demo model",
                        provider="openai-compatible",
                        model_name="demo-model",
                        base_url="https://model.example/v1",
                        temperature=0,
                        run_timeout_seconds=60,
                        status="tested",
                        is_active=False,
                    ),
                    SessionModel(id=session_id, title="Demo session"),
                ]
            )
            await db.flush()
            message = await MessageRepository(db).create(
                session_id=session_id,
                role=MessageRole.USER,
                content_text="统计数值",
            )
            run = await RunRepository(db).create(
                run_id=run_id,
                session_id=session_id,
                datasource_id=datasource_id,
                user_message_id=message.id,
                question="统计数值",
                idempotency_key=f"request_{suffix}",
                model_profile_id=profile_id,
                model_provider="openai-compatible",
                model_name="demo-model",
                schema_revision=1,
                datalink_graph_version="graph_1",
                run_timeout_seconds=60,
                input_snapshot_ref=f"snapshots/{run_id}/source.csv",
                final_output_mode="json_schema",
                model_capability_fingerprint="a" * 64,
            )
            events = RunEventPipeline(db)
            async with events.transaction(run.id):
                await events.append(RunEventCreate(run_id=run.id, type=RunEventType.RUN_QUEUED))
                await db.commit()
    finally:
        await engine.dispose()


class _NoLiveExecutor:
    """模拟重启后为空的执行器，不提供旧 Run 的内存上下文。"""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return False

    async def cancel_and_wait(self, run_id: str, *, timeout_seconds: float = 10) -> bool:
        del timeout_seconds
        self.cancelled.append(run_id)
        return False


class _FakeDataLink:
    """删除协作测试不访问网络，只返回与请求一致的确认结果。"""

    async def remove(self, datasource_id: str) -> DataLinkRemoveResult:
        return DataLinkRemoveResult(datasource_id=datasource_id, removed=True)


@pytest.mark.asyncio
async def test_recovery_finalizes_orphaned_runs_as_process_restarted(
    migrated_settings: Settings,
) -> None:
    """重启恢复不会重建私有执行上下文，只把遗留 Run 收成失败历史。"""

    await _seed_active_run(
        migrated_settings, run_id="run_restart", datasource_id="datasource_restart"
    )
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        recovered = await RunRecoveryService(
            session_factory=session_factory,
            notifier=RunEventNotifier(),
        ).recover()
        assert recovered == 1

        async with session_factory() as db:
            run = await db.get(RunModel, "run_restart")
            assert run is not None
            assert run.status == RunStatus.FAILED.value
            assert run.error_code == "PROCESS_RESTARTED"
            messages = list(
                await db.scalars(select(MessageModel).where(MessageModel.run_id == run.id))
            )
            assert [message.content_text for message in messages] == [
                "服务重启导致本次分析未完成。 服务重启导致分析中断"
            ]
            events = list(
                await db.scalars(
                    select(RunEventModel.event_type)
                    .where(RunEventModel.run_id == run.id)
                    .order_by(RunEventModel.seq)
                )
            )
            assert events == ["run.queued", "run.failed"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_datasource_delete_cancels_active_runs_before_tombstone(
    migrated_settings: Settings,
) -> None:
    """数据源先进入 deleting，活跃 Run 取消收尾后才写入 deleted tombstone。"""

    datasource_id = "datasource_deleting"
    await _seed_active_run(migrated_settings, run_id="run_deleting", datasource_id=datasource_id)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    executor = _NoLiveExecutor()
    lifecycle = RunLifecycleCoordinator(
        session_factory=session_factory,
        notifier=RunEventNotifier(),
        executor=executor,
    )
    try:
        async with session_factory() as db:
            service = DataSourceService(
                DataSourceRepository(db),
                gateway=object(),
                datalink_client=_FakeDataLink(),
                datasource_root=migrated_settings.datasource_root,
                max_upload_mb=migrated_settings.max_upload_mb,
                run_lifecycle=lifecycle,
            )
            result = await service.delete(datasource_id)
            await db.commit()
            assert result.status is DataSourceStatus.DELETED

        async with session_factory() as db:
            source = await db.get(DataSourceModel, datasource_id)
            run = await db.get(RunModel, "run_deleting")
            assert source is not None
            assert source.status == DataSourceStatus.DELETED.value
            assert run is not None
            assert run.status == RunStatus.CANCELED.value
            assert run.cancel_reason == "datasource_deleted"
            events = list(
                await db.scalars(
                    select(RunEventModel.event_type)
                    .where(RunEventModel.run_id == run.id)
                    .order_by(RunEventModel.seq)
                )
            )
            assert events == ["run.queued", "run.cancel_requested", "run.canceled"]
        assert executor.cancelled == ["run_deleting"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_delete_removes_only_registered_artifact_files(
    migrated_settings: Settings,
) -> None:
    """删除 Session 会清理登记文件并让 Run 历史随 Session 级联消失。"""

    datasource_id = "datasource_session_delete"
    run_id = "run_session_delete"
    await _seed_active_run(migrated_settings, run_id=run_id, datasource_id=datasource_id)
    storage_ref = f"runs/{run_id}/result.json"
    legacy_storage_ref = f"runs/{run_id}/legacy.json"
    artifact_path = migrated_settings.artifact_root / storage_ref
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text('{"safe":true}', encoding="utf-8")
    legacy_artifact_path = migrated_settings.artifact_root / legacy_storage_ref
    legacy_artifact_path.write_text('{"safe":true}', encoding="utf-8")

    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    lifecycle = RunLifecycleCoordinator(
        session_factory=session_factory,
        notifier=RunEventNotifier(),
        executor=_NoLiveExecutor(),
    )
    try:
        async with session_factory() as db:
            run = await db.get(RunModel, run_id)
            assert run is not None
            artifact = ArtifactModel(
                id="artifact_session_delete",
                run_id=run.id,
                session_id=run.session_id,
                tool_call_id=None,
                type="file",
                title="安全结果",
                storage_ref=storage_ref,
                mime_type="application/json",
                size_bytes=13,
                preview_json=None,
                metadata_json={"safe": True},
                content_hash="artifact-hash",
            )
            db.add(artifact)
            db.add(
                ArtifactModel(
                    id="artifact_session_delete_legacy",
                    run_id=run.id,
                    session_id=None,
                    tool_call_id=None,
                    type="file",
                    title="早期安全结果",
                    storage_ref=legacy_storage_ref,
                    mime_type="application/json",
                    size_bytes=13,
                    preview_json=None,
                    metadata_json={"safe": True},
                    content_hash="legacy-artifact-hash",
                )
            )
            await db.commit()

            service = SessionService(
                SessionRepository(db),
                DataSourceRepository(db),
                artifact_store=ArtifactStore(
                    ArtifactRepository(db), migrated_settings.artifact_root
                ),
                run_lifecycle=lifecycle,
            )
            await service.delete(run.session_id)
            await db.commit()

        assert not artifact_path.exists()
        assert not legacy_artifact_path.exists()
        async with session_factory() as db:
            assert await db.get(SessionModel, "session_session_delete") is None
            assert await db.get(RunModel, run_id) is None
            assert await db.get(ArtifactModel, "artifact_session_delete") is None
            assert await db.get(ArtifactModel, "artifact_session_delete_legacy") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_delete_queues_exact_artifact_ref_when_file_cleanup_fails(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文件删除失败不阻塞 Session 历史删除，并保存精确相对引用供后续重试。"""

    datasource_id = "datasource_cleanup_failure"
    run_id = "run_cleanup_failure"
    await _seed_active_run(migrated_settings, run_id=run_id, datasource_id=datasource_id)
    storage_ref = f"runs/{run_id}/result.json"

    async def fail_delete(_storage_ref: str) -> None:
        raise OSError("locked")

    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    lifecycle = RunLifecycleCoordinator(
        session_factory=session_factory,
        notifier=RunEventNotifier(),
        executor=_NoLiveExecutor(),
    )
    try:
        async with session_factory() as db:
            run = await db.get(RunModel, run_id)
            assert run is not None
            db.add(
                ArtifactModel(
                    id="artifact_cleanup_failure",
                    run_id=run.id,
                    session_id=run.session_id,
                    tool_call_id=None,
                    type="file",
                    title="安全结果",
                    storage_ref=storage_ref,
                    mime_type="application/json",
                    size_bytes=13,
                    preview_json=None,
                    metadata_json={"safe": True},
                    content_hash="artifact-hash",
                )
            )
            await db.commit()
            artifact_store = ArtifactStore(ArtifactRepository(db), migrated_settings.artifact_root)
            monkeypatch.setattr(artifact_store, "delete_registered_file", fail_delete)
            service = SessionService(
                SessionRepository(db),
                DataSourceRepository(db),
                artifact_store=artifact_store,
                run_lifecycle=lifecycle,
            )
            await service.delete(run.session_id)
            await db.commit()

        async with session_factory() as db:
            tasks = list(await db.scalars(select(ArtifactCleanupTaskModel)))
            assert len(tasks) == 1
            assert tasks[0].artifact_id is None
            assert tasks[0].storage_ref == storage_ref
            assert tasks[0].last_error_code == "ARTIFACT_DELETE_FAILED"
            assert await db.get(SessionModel, "session_cleanup_failure") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_delete_removes_session_with_historical_summary(
    migrated_settings: Settings,
) -> None:
    """有历史摘要的 Session 仍可删除；摘要、Run 与会话一并不可读。"""

    run_id = "run_summary_owner"
    datasource_id = "datasource_summary_owner"
    await _seed_active_run(migrated_settings, run_id=run_id, datasource_id=datasource_id)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    lifecycle = RunLifecycleCoordinator(
        session_factory=session_factory,
        notifier=RunEventNotifier(),
        executor=_NoLiveExecutor(),
    )
    try:
        async with session_factory() as db:
            run = await db.get(RunModel, run_id)
            assert run is not None
            session_id = run.session_id
            db.add(
                HistoricalAnswerSummaryModel(
                    id="history_summary_owner",
                    session_id=session_id,
                    datasource_id=run.datasource_id,
                    source_run_id=run.id,
                    source_run_finished_at=run.created_at,
                    topic="历史回答",
                    content_text="安全摘要",
                    data_freshness="not_queried",
                )
            )
            await db.commit()
            service = SessionService(
                SessionRepository(db),
                DataSourceRepository(db),
                artifact_store=ArtifactStore(
                    ArtifactRepository(db), migrated_settings.artifact_root
                ),
                run_lifecycle=lifecycle,
            )
            await service.delete(session_id)
            await db.commit()

        async with session_factory() as db:
            assert await db.get(SessionModel, session_id) is None
            assert await db.get(RunModel, run_id) is None
            assert await db.get(HistoricalAnswerSummaryModel, "history_summary_owner") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_datasource_tombstone_keeps_unread_session_history(
    migrated_settings: Settings,
) -> None:
    """数据源 tombstone 之后，未删 Session 的 Run 和历史摘要仍可读。"""

    run_id = "run_summary_readable"
    datasource_id = "datasource_summary_readable"
    await _seed_active_run(migrated_settings, run_id=run_id, datasource_id=datasource_id)
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    lifecycle = RunLifecycleCoordinator(
        session_factory=session_factory,
        notifier=RunEventNotifier(),
        executor=_NoLiveExecutor(),
    )
    try:
        async with session_factory() as db:
            run = await db.get(RunModel, run_id)
            assert run is not None
            session_id = run.session_id
            db.add(
                HistoricalAnswerSummaryModel(
                    id="history_summary_readable",
                    session_id=session_id,
                    datasource_id=datasource_id,
                    source_run_id=run.id,
                    source_run_finished_at=run.created_at,
                    topic="历史回答",
                    content_text="安全摘要",
                    data_freshness="not_queried",
                )
            )
            await db.commit()
            service = DataSourceService(
                DataSourceRepository(db),
                gateway=object(),
                datalink_client=_FakeDataLink(),
                datasource_root=migrated_settings.datasource_root,
                max_upload_mb=migrated_settings.max_upload_mb,
                run_lifecycle=lifecycle,
            )
            result = await service.delete(datasource_id)
            await db.commit()
            assert result.status is DataSourceStatus.DELETED

        async with session_factory() as db:
            source = await db.get(DataSourceModel, datasource_id)
            assert source is not None
            assert source.status == DataSourceStatus.DELETED.value
            assert await db.get(SessionModel, session_id) is not None
            assert await db.get(RunModel, run_id) is not None
            summary = await db.get(HistoricalAnswerSummaryModel, "history_summary_readable")
            assert summary is not None
            assert summary.session_id == session_id
            assert summary.source_run_id == run_id
    finally:
        await engine.dispose()
