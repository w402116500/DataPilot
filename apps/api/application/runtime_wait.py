"""为阶段四外部调用提供可取消、可限时的等待行为。"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable

from agent_runtime.ports import CancellationSignal


class RuntimeWaitCanceled(RuntimeError):
    """外部调用等待期间收到 Run 取消请求。"""


class RuntimeWaitTimedOut(TimeoutError):
    """外部调用超过调用方确定的剩余时长。"""


async def await_runtime_call[Result](
    operation: Awaitable[Result],
    *,
    cancellation: CancellationSignal,
    timeout_seconds: float,
) -> Result:
    """轮询同步取消信号，并在取消或超时时停止在途协程。"""

    if cancellation.is_cancelled():
        raise RuntimeWaitCanceled
    if timeout_seconds <= 0:
        raise RuntimeWaitTimedOut

    task = asyncio.ensure_future(operation)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            if cancellation.is_cancelled():
                raise RuntimeWaitCanceled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeWaitTimedOut
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=min(remaining, 0.05),
                )
            except TimeoutError:
                continue
    except (RuntimeWaitCanceled, RuntimeWaitTimedOut):
        task.cancel()
        await _bounded_task_cleanup(task)
        raise
    finally:
        if not task.done():
            task.cancel()
            await _bounded_task_cleanup(task)


async def _bounded_task_cleanup(task: asyncio.Task[object]) -> None:
    """取消收尾有界；不合作的 SDK 请求不能再次阻塞 Run 终态。"""

    if task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
    except TimeoutError:
        # The task owns the SDK request and will observe cancellation eventually.
        # Consume a late exception without keeping the Run waiting for it.
        task.add_done_callback(_consume_task_result)
    except (asyncio.CancelledError, Exception):
        return


def _consume_task_result(task: asyncio.Task[object]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()
