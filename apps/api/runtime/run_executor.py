"""单进程后台 Run 执行、超时与统一收尾编排。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from agent_runtime.contracts import AgentErrorCode
from application.run_execution import RunExecutionContext
from application.run_runtime import RunRuntimeError, RunRuntimeService
from contracts.run_events import RunEventCreate, RunEventType
from contracts.status import RunStatus
from metadata.repositories import RunRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from runtime.run_cancel_registry import RunCancellation, RunCancelRegistry
from runtime.run_event_pipeline import RunEventNotifier, RunEventPipeline
from runtime.run_finalizer import RunFinalizer

logger = logging.getLogger(__name__)
RuntimeFactory = Callable[[AsyncSession, RunEventPipeline], RunRuntimeService]
_CANCELLATION_GRACE_SECONDS: Final = 5


class RunExecutor:
    """在独立数据库会话中执行 Run；请求结束后绝不复用 HTTP Session。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: RunEventNotifier,
        registry: RunCancelRegistry,
        runtime_factory: RuntimeFactory,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._registry = registry
        self._runtime_factory = runtime_factory
        self._finalizer = RunFinalizer(session_factory, notifier)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def schedule(self, execution: RunExecutionContext) -> None:
        """仅在创建事务提交后调用；重复调度同一 Run 直接忽略。"""

        if execution.run_context.run_id not in self._tasks:
            self._tasks[execution.run_context.run_id] = asyncio.create_task(self.run(execution))

    def cancel(self, run_id: str) -> bool:
        """尽力向当前进程任务传递取消；持久化取消事实由 RunService 负责。"""

        return self._registry.cancel(run_id)

    async def cancel_and_wait(self, run_id: str, *, timeout_seconds: float = 10) -> bool:
        """通知当前任务并等待其 Finalizer 收尾；无响应时再强制停止 Task。"""

        self.cancel(run_id)
        task = self._tasks.get(run_id)
        if task is None:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return True

    async def run(self, execution: RunExecutionContext) -> None:
        """执行 Run 并将任何退出路径统一交给 Finalizer。"""

        run_id = execution.run_context.run_id
        cancellation = self._registry.register(run_id)
        try:
            start_result = await self._start(run_id)
            if start_result == "cancel_requested":
                await self._finalizer.fail(
                    run_id,
                    error_code="RUN_CANCELED",
                    error_message="分析已取消",
                    canceled=True,
                )
                return
            if start_result != "started":
                return
            outcome = await self._run_with_timeout(execution, cancellation)
        except _RunTimeout:
            cancellation.cancel()
            await self._finalizer.fail(
                run_id,
                error_code="RUN_TIMEOUT",
                error_message="Run 超过总时限，未形成可收尾的运行结果",
            )
        except RunRuntimeError as exc:
            if exc.failure.code is AgentErrorCode.RUN_CANCELED or cancellation.is_cancelled():
                await self._finalizer.fail(
                    run_id,
                    error_code="RUN_CANCELED",
                    error_message="分析已取消",
                    canceled=True,
                )
            else:
                await self._finalizer.fail(
                    run_id,
                    error_code=exc.failure.code.value,
                    error_message=exc.failure.message,
                )
        except asyncio.CancelledError:
            cancellation.cancel()
            await self._finalizer.fail(
                run_id,
                error_code="RUN_CANCELED",
                error_message="分析已取消",
                canceled=True,
            )
            raise
        except Exception:
            logger.exception("Run execution failed", extra={"run_id": run_id})
            await self._finalizer.fail(
                run_id,
                error_code="RUNTIME_START_FAILED",
                error_message="分析运行时启动或执行失败",
            )
        else:
            if cancellation.is_cancelled():
                await self._finalizer.fail(
                    run_id,
                    error_code="RUN_CANCELED",
                    error_message="分析已取消",
                    canceled=True,
                )
            else:
                await self._finalizer.complete(run_id, outcome)
        finally:
            self._registry.unregister(run_id)
            self._tasks.pop(run_id, None)

    async def _start(self, run_id: str) -> str:
        """从 queued 切到 running 并写事件；已经取消或终态的 Run 不会被重新启动。"""

        async with self._session_factory() as db:
            events = RunEventPipeline(db, notify=self._notifier.notify)
            async with events.transaction(run_id):
                run = await RunRepository(db).get(run_id)
                if run is None or run.status != RunStatus.QUEUED.value:
                    return "not_startable"
                if run.cancel_requested_at is not None:
                    return "cancel_requested"
                run.status = RunStatus.RUNNING.value
                run.started_at = datetime.now(UTC)
                await events.append(RunEventCreate(run_id=run.id, type=RunEventType.RUN_STARTED))
                await db.commit()
        await self._notifier.notify(run_id)
        return "started"

    async def _run_with_timeout(
        self,
        execution: RunExecutionContext,
        cancellation: RunCancellation,
    ):
        """总超时覆盖整个 Graph，而不只限制单次模型调用。"""

        async with self._session_factory() as db:
            events = RunEventPipeline(db, notify=self._notifier.notify)
            runtime = self._runtime_factory(db, events)
            task = asyncio.create_task(runtime.execute(execution, cancellation=cancellation))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task), timeout=execution.model.run_timeout_seconds
                )
            except TimeoutError as exc:
                cancellation.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=_CANCELLATION_GRACE_SECONDS
                    )
                except TimeoutError:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                except (asyncio.CancelledError, Exception):
                    # Run 已经过总时限，无论清理阶段自行报什么错误都收为超时。
                    pass
                raise _RunTimeout from exc


class _RunTimeout(Exception):
    """内部总超时标识，不直接暴露为 API 异常。"""
