"""Run 创建、幂等读取与进程内调度的应用层服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from application.run_execution import RunExecutionContext
from contracts.api import Page, PageResult
from contracts.errors import AppError, ErrorCode
from contracts.ids import make_id
from contracts.run_events import RunEventCreate, RunEventType
from contracts.runs import RunCreate, RunCreateAccepted, RunRead
from contracts.status import DataSourceStatus, MessageRole, RunStatus
from metadata.models import RunModel, SessionModel
from metadata.repositories import (
    DataSourceRepository,
    MessageRepository,
    RunRepository,
    SessionRepository,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.conversation_memory import ConversationMemoryService
from runtime.run_context_resolver import RunContextResolver
from runtime.run_event_pipeline import RunEventPipeline

RunScheduler = Callable[[RunExecutionContext], None]
RunCanceler = Callable[[str], bool]


class RunService:
    """创建与读取 Run；后台执行只接收已解析的进程内上下文。"""

    def __init__(
        self,
        *,
        db: AsyncSession,
        resolver: RunContextResolver,
        events: RunEventPipeline,
        schedule: RunScheduler,
        request_cancel: RunCanceler | None = None,
    ) -> None:
        self._db = db
        self._resolver = resolver
        self._events = events
        self._schedule = schedule
        self._request_cancel = request_cancel or (lambda _run_id: False)

    async def create(self, session_id: str, payload: RunCreate) -> RunCreateAccepted:
        """原子创建消息、Run 与 queued 事件，成功提交后才通知并排入后台。"""

        run_id = make_id("run")
        execution: RunExecutionContext
        session: SessionModel
        try:
            async with self._events.transaction(run_id):
                session = await SessionRepository(self._db).get_for_update(session_id)
                if session is None:
                    raise AppError(
                        ErrorCode.SESSION_NOT_FOUND,
                        "会话不存在",
                        status_code=404,
                        details={"session_id": session_id},
                    )
                existing = await RunRepository(self._db).get_by_session_and_idempotency_key(
                    session_id, payload.idempotency_key
                )
                if existing is not None:
                    return self._reuse_or_conflict(existing, payload.question)
                active = await RunRepository(self._db).get_active_for_session(session_id)
                if active is not None:
                    raise AppError(
                        ErrorCode.RUN_ALREADY_ACTIVE,
                        "当前会话已有正在执行的分析",
                        status_code=409,
                        details={"run_id": active.id},
                    )
                messages = MessageRepository(self._db)
                high_water_before_run = await messages.max_position(session.id)
                execution = await self._resolver.resolve(
                    run_id=run_id,
                    session=session,
                    question=payload.question,
                    high_water_before_run=high_water_before_run,
                )
                run = await RunRepository(self._db).create(
                    run_id=run_id,
                    session_id=session.id,
                    datasource_id=execution.run_context.datasource_id,
                    user_message_id=None,
                    question=payload.question,
                    idempotency_key=payload.idempotency_key,
                    model_profile_id=execution.model.profile_id,
                    model_provider=execution.model.provider,
                    model_name=execution.model.model_name,
                    schema_revision=execution.run_context.schema_revision,
                    connection_revision=execution.run_context.connection_revision,
                    datalink_graph_version=execution.run_context.datalink_graph_version,
                    run_timeout_seconds=execution.model.run_timeout_seconds,
                    final_output_mode=execution.model.final_output_mode,
                    model_capability_fingerprint=execution.model.model_capability_fingerprint,
                    input_snapshot_ref=execution.run_context.input_snapshot_ref,
                    mask_fields=execution.run_context.mask_fields,
                )
                message = await messages.create(
                    session_id=session.id,
                    role=MessageRole.USER,
                    content_text=payload.question,
                    datasource_id=execution.run_context.datasource_id,
                    run_id=run.id,
                    position=high_water_before_run + 1,
                )
                run.user_message_id = message.id
                run.session_preferences_revision = (
                    execution.run_context.session_preferences_revision
                )
                run.datasource_context_revision = execution.run_context.datasource_context_revision
                run.context_through_message_position = (
                    execution.run_context.context_through_message_position
                )
                run.pending_id_snapshot = execution.run_context.pending_id
                run.pending_revision_snapshot = execution.run_context.pending_revision
                run.context_projection_version = execution.run_context.context_projection_version
                run.context_snapshot_hash = execution.run_context.context_snapshot_hash
                run.context_load_status = execution.run_context.context_load_status
                run.historical_summary_ids_json = execution.run_context.historical_summary_ids
                # Selection is not proof that Opening serialized a summary;
                # the runtime records the emitted count after the request.
                run.historical_summary_count = 0
                run.context_window_tokens = execution.model.context_window_tokens
                run.context_window_source = execution.model.context_window_source
                run.input_budget_tokens = execution.model.input_budget_tokens
                run.output_budget_tokens = execution.model.output_budget_tokens
                run.system_budget_tokens = execution.model.system_budget_tokens
                run.tool_schema_cost = execution.model.tool_schema_cost
                run.safety_margin_tokens = execution.model.safety_margin_tokens
                # Resolver selection is not proof that a historical summary
                # was serialized into a model request. Runtime calls update
                # this projection only after recording the emitted summary IDs.
                run.historical_context_injected = False
                await ConversationMemoryService(self._db).save_preference_from_user_message(
                    session_id=session.id,
                    position=message.position,
                    content_text=message.content_text,
                )
                session.last_message_at = datetime.now(UTC)
                await self._events.append(
                    RunEventCreate(run_id=run.id, type=RunEventType.RUN_QUEUED)
                )
                await self._db.commit()
        except IntegrityError:
            # 两个请求可能在应用层检查后同时进入；数据库约束是最后一道保护。
            await self._db.rollback()
            existing = await RunRepository(self._db).get_by_session_and_idempotency_key(
                session_id, payload.idempotency_key
            )
            if existing is not None:
                return self._reuse_or_conflict(existing, payload.question)
            active = await RunRepository(self._db).get_active_for_session(session_id)
            if active is not None:
                raise AppError(
                    ErrorCode.RUN_ALREADY_ACTIVE,
                    "当前会话已有正在执行的分析",
                    status_code=409,
                    details={"run_id": active.id},
                ) from None
            raise

        await self._events.notify_committed(run.id)
        self._schedule(execution)
        return RunCreateAccepted(run_id=run.id, session_id=session.id, status=RunStatus.QUEUED)

    async def get(self, run_id: str) -> RunRead:
        """读取已持久化 Run，不触发任何运行依赖。"""

        model = await RunRepository(self._db).get(run_id)
        if model is None:
            raise AppError(ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404)
        return to_run_read(
            model, datasource_deleted=await self._datasource_deleted(model.datasource_id)
        )

    async def cancel(self, run_id: str, *, reason: str):
        """先保存取消命令和事件，再向当前进程任务发协作取消信号。"""

        async with self._events.transaction(run_id):
            run = await RunRepository(self._db).get(run_id)
            if run is None:
                raise AppError(ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404)
            if run.status in {
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELED.value,
            }:
                return _to_cancel_read(run)
            if run.cancel_requested_at is None:
                run.cancel_requested_at = datetime.now(UTC)
                run.cancel_reason = reason
                await self._events.append(
                    RunEventCreate(
                        run_id=run.id,
                        type=RunEventType.RUN_CANCEL_REQUESTED,
                        payload={"reason_code": reason},
                    )
                )
                await self._db.commit()
                notify = True
            else:
                notify = False
            result = _to_cancel_read(run)
        if notify:
            await self._events.notify_committed(run_id)
        self._request_cancel(run_id)
        return result

    async def list_for_session(
        self, session_id: str, page: Page, *, q: str | None = None
    ) -> PageResult[RunRead]:
        """按创建时间倒序读取会话历史，不重放或续跑旧 Run。"""

        await self._get_session(session_id)
        query = None if q is None else (q.strip() or None)
        items, total = await RunRepository(self._db).list_for_session(
            session_id,
            offset=page.offset,
            limit=page.page_size,
            q=query,
        )
        return PageResult(
            items=[
                to_run_read(
                    item,
                    datasource_deleted=await self._datasource_deleted(item.datasource_id),
                )
                for item in items
            ],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def _get_session(self, session_id: str) -> SessionModel:
        session = await SessionRepository(self._db).get(session_id)
        if session is None:
            raise AppError(
                ErrorCode.SESSION_NOT_FOUND,
                "会话不存在",
                status_code=404,
                details={"session_id": session_id},
            )
        return session

    async def _datasource_deleted(self, datasource_id: str | None) -> bool:
        """历史 Run 只标示已完成的 DataSource 删除，不把 deleting 误报为已删除。"""

        if datasource_id is None:
            return False
        datasource = await DataSourceRepository(self._db).get(datasource_id)
        return datasource is not None and datasource.status == DataSourceStatus.DELETED.value

    @staticmethod
    def _reuse_or_conflict(existing: RunModel, question: str) -> RunCreateAccepted:
        if existing.question != question:
            raise AppError(
                ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
                "同一幂等键不能用于不同问题",
                status_code=409,
                details={"run_id": existing.id},
            )
        return RunCreateAccepted(
            run_id=existing.id,
            session_id=existing.session_id,
            status=RunStatus(existing.status),
        )


def to_run_read(model: RunModel, *, datasource_deleted: bool = False) -> RunRead:
    """把 Run ORM 模型映射为不含密钥、快照或 GraphState 的历史视图。"""

    return RunRead(
        id=model.id,
        session_id=model.session_id,
        datasource_id=model.datasource_id,
        datasource_deleted=datasource_deleted,
        user_message_id=model.user_message_id,
        question=model.question,
        status=RunStatus(model.status),
        protocol_id=model.protocol_id,
        model_profile_id=model.model_profile_id,
        model_provider=model.model_provider,
        model_name=model.model_name,
        schema_revision=model.schema_revision,
        datalink_graph_version=model.datalink_graph_version,
        has_input_snapshot=model.input_snapshot_ref is not None,
        connection_revision=model.connection_revision,
        run_timeout_seconds=model.run_timeout_seconds,
        completion_kind=model.completion_kind,
        incomplete_reason=model.incomplete_reason,
        error_code=model.error_code,
        error_message=model.error_message,
        cancel_requested_at=model.cancel_requested_at,
        cancel_reason=model.cancel_reason,
        started_at=model.started_at,
        finished_at=model.finished_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
        answer_data_freshness=model.answer_data_freshness,
        historical_context_injected=bool(model.historical_context_injected),
        historical_summary_count=model.historical_summary_count or 0,
    )


def _to_cancel_read(model: RunModel):
    """取消接口只返回状态与登记时间，不能带入运行时诊断。"""

    from contracts.runs import RunCancelRead

    return RunCancelRead(
        run_id=model.id,
        status=RunStatus(model.status),
        cancel_requested_at=model.cancel_requested_at,
    )
