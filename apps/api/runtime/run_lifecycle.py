"""跨路由和删除流程协调 Run 取消、等待与服务重启恢复。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from contracts.run_events import RunEventCreate, RunEventType
from contracts.runs import RunCancelRead
from contracts.status import RunStatus
from metadata.repositories import RunRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from runtime.run_event_pipeline import RunEventNotifier, RunEventPipeline
from runtime.run_finalizer import RunFinalizer

_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELED.value,
    }
)


class RunExecutionController(Protocol):
    """删除协调所需的最小执行器能力，便于测试且不反向耦合具体 Runtime。"""

    def cancel(self, run_id: str) -> bool: ...

    async def cancel_and_wait(self, run_id: str, *, timeout_seconds: float = 10) -> bool: ...


class RunLifecycleCoordinator:
    """供 Session/DataSource 删除复用的取消协调器；终态始终交给 Finalizer。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: RunEventNotifier,
        executor: RunExecutionController,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._executor = executor
        self._finalizer = RunFinalizer(session_factory, notifier)

    async def cancel_for_datasource(self, datasource_id: str, *, reason: str) -> None:
        """取消引用指定数据源的所有活跃 Run，并等待每条都写完终态。"""

        async with self._session_factory() as db:
            run_ids = [
                run.id for run in await RunRepository(db).list_active_for_datasource(datasource_id)
            ]
        await self._cancel_all(run_ids, reason=reason)

    async def cancel_for_session(self, session_id: str, *, reason: str) -> None:
        """取消会话唯一活跃 Run；数据库约束保证最多一条。"""

        async with self._session_factory() as db:
            active = await RunRepository(db).get_active_for_session(session_id)
            run_ids = [] if active is None else [active.id]
        await self._cancel_all(run_ids, reason=reason)

    async def _cancel_all(self, run_ids: list[str], *, reason: str) -> None:
        for run_id in run_ids:
            await self._request_cancel(run_id, reason=reason)
            if not await self._executor.cancel_and_wait(run_id):
                # 当前进程没有该 Run 的私有上下文，不能尝试续跑；直接收尾。
                await self._finalizer.fail(
                    run_id,
                    error_code="RUN_CANCELED",
                    error_message="分析已取消",
                    canceled=True,
                )

    async def _request_cancel(self, run_id: str, *, reason: str) -> RunCancelRead | None:
        """先持久化取消命令和事件，提交成功后才通知后台任务。"""

        async with self._session_factory() as db:
            events = RunEventPipeline(db, notify=self._notifier.notify)
            async with events.transaction(run_id):
                run = await RunRepository(db).get(run_id)
                if run is None or run.status in _TERMINAL_STATUSES:
                    return None
                if run.cancel_requested_at is None:
                    run.cancel_requested_at = datetime.now(UTC)
                    run.cancel_reason = reason
                    await events.append(
                        RunEventCreate(
                            run_id=run.id,
                            type=RunEventType.RUN_CANCEL_REQUESTED,
                            payload={"reason_code": reason},
                        )
                    )
                    await db.commit()
                    notify = True
                else:
                    notify = False
                result = RunCancelRead(
                    run_id=run.id,
                    status=RunStatus(run.status),
                    cancel_requested_at=run.cancel_requested_at,
                )
        if notify:
            await events.notify_committed(run_id)
        return result


class RunRecoveryService:
    """服务启动时把没有进程内上下文的活跃 Run 统一收为重启失败。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: RunEventNotifier,
    ) -> None:
        self._session_factory = session_factory
        self._finalizer = RunFinalizer(session_factory, notifier)

    async def recover(self) -> int:
        """只读取遗留 Run ID，不恢复模型、密钥、GraphState 或 SQL。"""

        async with self._session_factory() as db:
            run_ids = [run.id for run in await RunRepository(db).list_interrupted()]
        recovered = 0
        for run_id in run_ids:
            if await self._finalizer.fail(
                run_id,
                error_code="PROCESS_RESTARTED",
                error_message="服务重启导致分析中断",
            ):
                recovered += 1
        return recovered
